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
 private void verifyCurrentSources(Actor a,String owner){
  var run=db.one("SELECT status FROM qa_run WHERE tenant_id=:t AND id=:id",params("t",a.tenantId(),"id",owner));if(!Set.of("QUEUED","RUNNING").contains(run.get("status").toString()))throw new ApiException(409,"CANCELLED","请求已停止");
  if(!db.list("SELECT s.id FROM run_source s JOIN document d ON d.tenant_id=s.tenant_id AND d.id=s.document_id WHERE s.tenant_id=:t AND s.run_id=:r AND s.purpose<>'HISTORY' AND (d.active_build_id IS NULL OR d.active_build_id<>s.build_id)",params("t",a.tenantId(),"r",owner)).isEmpty())throw new ApiException(409,"SOURCE_CHANGED","来源版本已变化");
 }
 private List<String> jobSources(Actor a,String owner,BigDecimal estimate){
  var job=db.one("SELECT * FROM job WHERE tenant_id=:t AND id=:id",params("t",a.tenantId(),"id",owner));if(!"RUNNING".equals(job.get("status"))||job.get("cancel_requested_at")!=null)throw new ApiException(409,"CANCELLED","后台任务已停止");
  String kind=Objects.toString(job.get("kind"),"");List<String>docs=new ArrayList<>();
  if(Set.of("INGEST","RESTORE").contains(kind)){
   var build=db.one("SELECT b.document_id,b.fence,d.lifecycle_epoch,d.status FROM index_build b JOIN document d ON d.tenant_id=b.tenant_id AND d.id=b.document_id WHERE b.tenant_id=:t AND b.id=:id",params("t",a.tenantId(),"id",job.get("target_id")));
   if(!"LIVE".equals(build.get("status"))||!Objects.equals(((Number)build.get("fence")).longValue(),((Number)build.get("lifecycle_epoch")).longValue()))throw new ApiException(409,"SOURCE_CHANGED","入库来源生命周期已变化");docs.add(build.get("document_id").toString());
  }else if(kind.equals("EVALUATE")){
   for(var row:db.list("SELECT DISTINCT d.document_id FROM eval_run e JOIN eval_snapshot_document d ON d.tenant_id=e.tenant_id AND d.snapshot_id=e.snapshot_id WHERE e.tenant_id=:t AND e.job_id=:id UNION SELECT DISTINCT s.document_id FROM eval_result r JOIN eval_run e ON e.tenant_id=r.tenant_id AND e.id=r.eval_run_id JOIN run_source s ON s.tenant_id=r.tenant_id AND s.run_id=r.qa_run_id WHERE e.tenant_id=:t AND e.job_id=:id",params("t",a.tenantId(),"id",owner)))docs.add(row.get("document_id").toString());
  }else throw new ApiException(400,"INVALID_MODEL_OWNER","此后台任务不允许调用模型");
  return docs;
 }
 private Map<String,Object> call(Actor a,String kind,String owner,String purpose,String model,String path,Map<String,Object>body,int output){
  for(int attempt=1;attempt<=3;attempt++){try{return callOnce(a,kind,owner,purpose,model,path,body,output,attempt);}catch(ApiException e){if(!e.code.equals("MODEL_RETRYABLE"))throw e;if(attempt==3)throw new ApiException(502,"MODEL_PROVIDER_ERROR","模型供应商有限重试仍失败");try{Thread.sleep(attempt*100L);}catch(InterruptedException interrupted){Thread.currentThread().interrupt();throw new ApiException(409,"CANCELLED","调用重试已中断");}}}throw new IllegalStateException();
 }
 private Map<String,Object> callOnce(Actor a,String kind,String owner,String purpose,String model,String path,Map<String,Object>body,int output,int attempt){
  if(base.isBlank()||key.isBlank()||model.isBlank())throw new ApiException(503,"MODEL_NOT_CONFIGURED","未配置对应真实模型服务");
  if(!Set.of("RUN","JOB").contains(kind)||output<0||output>6000)throw new ApiException(400,"INVALID_INPUT","模型调用参数无效");
  String json=Json.write(body);long input=json.getBytes(StandardCharsets.UTF_8).length; // Conservative bound, not a tokenizer claim.
  if(input>120000)throw new ApiException(413,"INPUT_TOO_LARGE","模型输入超过请求硬限制");
  if(!slots.tryAcquire())throw new ApiException(429,"RATE_LIMITED","模型在途并发已满");String callId=Json.id();BudgetAuthority.Reservation reservation=null;boolean sent=false;
  try{
   var prices=budgets.pricing(a);BigDecimal estimate=BudgetService.estimate(prices,input,output);
   List<String>docs=new ArrayList<>();Duration timeout=Duration.ofSeconds(90);String batchJobId=kind.equals("JOB")?owner:null;
   if(kind.equals("RUN")){
    var run=db.one("SELECT * FROM qa_run WHERE tenant_id=:t AND id=:id AND owner_id=:u",params("t",a.tenantId(),"id",owner,"u",a.userId()));
    if(!Set.of("QUEUED","RUNNING").contains(run.get("status").toString()))throw new ApiException(409,"CANCELLED","请求已停止");
    int maxCalls=run.get("mode").equals("AGENT")?12:8;
    var usage=db.one("SELECT COUNT(*) AS calls,COALESCE(SUM(COALESCE(input_tokens,JSON_EXTRACT(usage_detail,'$.inputUpperBound'))),0) AS input_total,COALESCE(SUM(COALESCE(output_tokens,JSON_EXTRACT(usage_detail,'$.outputUpperBound'))),0) AS output_total FROM model_call WHERE tenant_id=:t AND run_id=:r",params("t",a.tenantId(),"r",owner));
    if(((Number)usage.get("calls")).intValue()>=maxCalls||new BigDecimal(usage.get("input_total").toString()).longValue()+input>30000||new BigDecimal(usage.get("output_total").toString()).longValue()+output>6000)throw new ApiException(429,"BUDGET_EXCEEDED","请求全局调用或 token 上界已耗尽");
    var budget=Json.object(run.get("budget_json").toString());batchJobId=Objects.toString(budget.get("evaluationJobId"),null);long left=Json.integer(budget,"deadlineSeconds",run.get("mode").equals("AGENT")?60:45)*1000L-(System.currentTimeMillis()-Long.parseLong(budget.getOrDefault("startedEpochMs",System.currentTimeMillis()).toString()));if(left<=0)throw new ApiException(429,"BUDGET_EXCEEDED","请求时限已耗尽");timeout=Duration.ofMillis(Math.min(45000,left));
    for(var r:db.list("SELECT DISTINCT document_id FROM run_source WHERE tenant_id=:t AND run_id=:r",params("t",a.tenantId(),"r",owner)))docs.add(r.get("document_id").toString());
   }else{docs.addAll(jobSources(a,owner,estimate));}
   if(batchJobId!=null)budgets.requireBatchRunning(a,batchJobId);
   reservation=authority.reserve(a,callId,input,output,prices);estimate=reservation.amount();
   var callRow=params("tenant_id",a.tenantId(),"id",callId,"run_id",kind.equals("RUN")?owner:null,"job_id",kind.equals("JOB")?owner:null,"purpose",purpose,"model_config_id",prices.get("configId"),"attempt_no",attempt,"status","RESERVED","reserved_amount",estimate,"pricing_snapshot",Json.write(reservation.price()),"usage_detail",Json.write(params("inputUpperBound",input,"outputUpperBound",output)),"reservation_authority",reservation.authority(),"reservation_id",reservation.id());if(batchJobId==null)db.insert("model_call",callRow);else budgets.recordBatchCall(a,batchJobId,callRow);
   var request=HttpRequest.newBuilder(URI.create(base+path)).timeout(timeout).header("Authorization","Bearer "+key).header("Content-Type","application/json").POST(HttpRequest.BodyPublishers.ofString(json)).build();
   // Authority gate includes every source sent previously, including rerank and history dependencies.
   final boolean[] dispatched={false};final String linkedBatch= batchJobId;HttpResponse<String>response;
   try{response=access.guard(a,docs,()->{if(kind.equals("RUN"))verifyCurrentSources(a,owner);else jobSources(a,owner,BigDecimal.ZERO);if(linkedBatch!=null)budgets.requireBatchRunning(a,linkedBatch);dispatched[0]=true;try{return http.send(request,HttpResponse.BodyHandlers.ofString());}catch(InterruptedException e){Thread.currentThread().interrupt();throw new ApiException(503,"MODEL_TIMEOUT","模型请求中断");}catch(Exception e){throw new ApiException(503,"MODEL_TIMEOUT","模型请求超时或连接失败");}});}finally{sent=dispatched[0];}
   if(response.statusCode()==429||response.statusCode()>=500)throw new ApiException(502,"MODEL_RETRYABLE","模型服务暂不可用");if(response.statusCode()<200||response.statusCode()>=300)throw new ApiException(502,"MODEL_PROVIDER_ERROR","模型供应商请求失败");
   var result=Json.object(response.body());Object usageRaw=result.get("usage");
   if(usageRaw instanceof Map<?,?>usage&&usage.get("prompt_tokens") instanceof Number in){long out=usage.get("completion_tokens") instanceof Number n?n.longValue():0;BigDecimal actual=BudgetService.estimate(reservation.price(),in.longValue(),out);authority.settle(a,reservation,"SETTLED",in.longValue(),out);db.exec("UPDATE model_call SET status='SETTLED',settled_amount=:a,input_tokens=:i,output_tokens=:o,provider_request_id=:p,usage_detail=:d WHERE tenant_id=:t AND id=:id",params("a",actual,"i",in.longValue(),"o",out,"p",response.headers().firstValue("x-request-id").orElse(null),"d",Json.write(usage),"t",a.tenantId(),"id",callId));}
   else{authority.settle(a,reservation,"UNKNOWN",null,null);db.exec("UPDATE model_call SET status='UNKNOWN' WHERE tenant_id=:t AND id=:id",params("t",a.tenantId(),"id",callId));}
   if(kind.equals("RUN"))db.exec("UPDATE run_source SET send_status='SENT' WHERE tenant_id=:t AND run_id=:id AND send_status='PREPARED'",params("t",a.tenantId(),"id",owner));return result;
  }catch(RuntimeException e){if(reservation!=null){try{authority.settle(a,reservation,sent?"UNKNOWN":"NOT_SENT",null,null);}catch(RuntimeException reconciliation){e.addSuppressed(reconciliation);}db.exec("UPDATE model_call SET status=:s,settled_amount=CASE WHEN :s='NOT_SENT' THEN 0 ELSE settled_amount END WHERE tenant_id=:t AND id=:id AND status='RESERVED'",params("s",sent?"UNKNOWN":"NOT_SENT","t",a.tenantId(),"id",callId));}throw e;}finally{slots.release();}
 }
}

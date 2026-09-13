package dev.rag.search;
import dev.rag.common.*;
import dev.rag.identity.AccessPolicy;
import dev.rag.model.ModelGateway;
import org.springframework.stereotype.Service;
import org.springframework.beans.factory.annotation.Value;
import java.net.*;
import java.net.http.*;
import java.nio.*;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Duration;
import java.util.*;
import static dev.rag.common.Db.params;
@Service
public class SearchService {
 public record Context(List<Map<String,Object>> evidence,Map<String,Object>trace,List<String>warnings){}
 private final Db db;private final AccessPolicy access;private final ModelGateway model;private final String es,prefix;
 private final HttpClient http=HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();
 public SearchService(Db db,AccessPolicy access,ModelGateway model,@Value("${rag.elasticsearch-url}")String es,@Value("${rag.index-prefix}")String prefix){this.db=db;this.access=access;this.model=model;this.es=es;this.prefix=prefix;}
 @SuppressWarnings("unchecked") public static Map<String,Object> part(Map<String,Object>config,String name){Object p=config.get(name);return p instanceof Map<?,?>?(Map<String,Object>)p:Map.of();}
 public static void validateConfig(Map<String,Object>c){
  var q=part(c,"query");var r=part(c,"retrieval");var p=part(c,"post");String mode=r.getOrDefault("mode","BM25").toString();
  if(!Set.of("BM25","SPARSE","DENSE","HYBRID").contains(mode)||Json.integer(q,"maxVariants",4)<1||Json.integer(q,"maxVariants",4)>4||Json.integer(r,"perRouteK",50)<1||Json.integer(r,"perRouteK",50)>100||Json.integer(r,"numCandidates",200)<Json.integer(r,"perRouteK",50)||Json.integer(r,"numCandidates",200)>1000||Json.integer(part(c,"context"),"maxTokens",6000)<100||Json.integer(part(c,"context"),"maxTokens",6000)>12000||Json.integer(part(c,"context"),"maxUnits",6)<1||Json.integer(part(c,"context"),"maxUnits",6)>20)throw new ApiException(400,"INVALID_CONFIG","检索参数超出支持范围");
  if(Json.bool(q,"hyde",false)&&!Set.of("DENSE","HYBRID").contains(mode))throw new ApiException(400,"INVALID_CONFIG","HyDE 需要 Dense 通道");
  Object reps=r.getOrDefault("representations",List.of("ORIGINAL"));if(!(reps instanceof List<?> list)||list.isEmpty()||!Set.of("ORIGINAL","SUMMARY","QUESTION").containsAll(list))throw new ApiException(400,"INVALID_CONFIG","表示类型无效");
  double lambda=Double.parseDouble(p.getOrDefault("lambda",.7).toString());if(!Double.isFinite(lambda)||lambda<0||lambda>1)throw new ApiException(400,"INVALID_CONFIG","MMR lambda 无效");
  if(Json.integer(part(c,"fusion"),"rrfK",60)<1||Json.integer(part(c,"fusion"),"rrfK",60)>1000||Json.integer(p,"rerankTopN",30)<1||Json.integer(p,"rerankTopN",30)>30)throw new ApiException(400,"INVALID_CONFIG","融合或重排容量无效");
  for(String section:List.of("query","post","answer"))for(var entry:part(c,section).entrySet())if(Set.of("rewrite","hyde","multiQuery","rerank","allowRerankFallback","mmr","parent","strict","allowRewriteFallback").contains(entry.getKey())&&!(entry.getValue() instanceof Boolean))throw new ApiException(400,"INVALID_CONFIG","策略开关必须是 boolean");
 }
 public void validateCapabilities(Actor actor,Map<String,Object>config){validateConfig(config);var retrieval=part(config,"retrieval");var post=part(config,"post");boolean vector=Set.of("DENSE","HYBRID").contains(retrieval.getOrDefault("mode","BM25"))||Json.bool(post,"mmr",false);if(!vector)return;var builds=access.readableBuilds(actor,List.of());if(builds.isEmpty())return;var reps=(List<?>)retrieval.getOrDefault("representations",List.of("ORIGINAL"));for(var b:db.list("SELECT capabilities,embedding_dims,model_fingerprint FROM index_build WHERE tenant_id=:t AND id IN (:ids)",params("t",actor.tenantId(),"ids",builds))){for(Object rep:reps)if(!b.get("capabilities").toString().contains(rep.toString()))throw new ApiException(409,"INDEX_CAPABILITY_MISSING","当前索引缺少配置要求的表示");if(b.get("embedding_dims")==null||((Number)b.get("embedding_dims")).intValue()!=model.dimensions()||!Objects.equals(b.get("model_fingerprint"),model.embeddingModel()))throw new ApiException(409,"INDEX_CAPABILITY_MISSING","当前索引向量模型不兼容");}}
 @SuppressWarnings("unchecked")
 public Context search(Actor actor,String runId,String question,List<String>kbIds,Map<String,Object>config,List<Map<String,Object>>history){
  validateConfig(config);if(question==null||question.isBlank()||question.length()>2000)throw new ApiException(400,"INVALID_INPUT","问题须为 1 至 2000 字符");
  var builds=access.readableBuilds(actor,kbIds);if(builds.size()>10000)throw new ApiException(409,"CAPACITY_LIMIT","可读索引超出当前检索容量");
  List<String>warnings=new ArrayList<>();Map<String,Object>trace=new LinkedHashMap<>();if(builds.isEmpty())return new Context(List.of(),trace,warnings);
  var query=part(config,"query");var retrieval=part(config,"retrieval");var post=part(config,"post");String mode=retrieval.getOrDefault("mode","BM25").toString();boolean dense=Set.of("DENSE","HYBRID").contains(mode),sparse=!mode.equals("DENSE");
  List<String>reps=(List<String>)retrieval.getOrDefault("representations",List.of("ORIGINAL"));
  if(dense||Json.bool(post,"mmr",false))for(var b:db.list("SELECT * FROM index_build WHERE tenant_id=:t AND id IN (:ids)",params("t",actor.tenantId(),"ids",builds))){
   String cap=b.get("capabilities").toString();for(String rep:reps)if(!cap.contains(rep))throw new ApiException(409,"INDEX_CAPABILITY_MISSING","当前索引缺少所需表示");
   if(b.get("embedding_dims")==null||((Number)b.get("embedding_dims")).intValue()!=model.dimensions()||!Objects.toString(b.get("model_fingerprint"),"").equals(model.embeddingModel()))throw new ApiException(409,"INDEX_CAPABILITY_MISSING","索引模型或维度与查询模型不一致");
  }
  LinkedHashSet<String>texts=new LinkedHashSet<>();texts.add(question.strip());String independent=question;
  if(Json.bool(query,"rewrite",false)&&!history.isEmpty()){
   var messages=new ArrayList<Map<String,Object>>();messages.add(params("role","system","content","将追问改写为独立检索问题。历史仅解析指代，不是新事实。保留否定、错误码及条件。只输出 JSON {query:string}。"));messages.addAll(history);messages.add(params("role","user","content",question));
   try{independent=Json.str(Json.object(content(model.chat(actor,"RUN",runId,"REWRITE",messages,300))),"query");if(independent.isBlank()||independent.length()>2000)throw new ApiException(422,"INVALID_MODEL_RESPONSE","改写无效");texts.add(independent);}catch(ApiException e){if(Json.bool(query,"allowRewriteFallback",false)&&Set.of("MODEL_TIMEOUT","MODEL_PROVIDER_ERROR","INVALID_MODEL_RESPONSE","INVALID_JSON").contains(e.code))warnings.add("REWRITE_SKIPPED");else throw e;}
  }
  if(Json.bool(query,"multiQuery",false)){
   var output=Json.object(content(model.chat(actor,"RUN",runId,"MULTI_QUERY",List.of(params("role","system","content","为检索生成最多两个不改变条件的问法。只输出 JSON {queries:[string,string]}。"),params("role","user","content",independent)),400)));
   if(!(output.get("queries") instanceof List<?> variants)||variants.size()>2)throw new ApiException(422,"INVALID_MODEL_RESPONSE","多查询响应无效");for(Object v:(List<?>)output.get("queries")){String text=v.toString().strip().replaceAll("\\s+"," ");if(text.isBlank()||text.length()>2000)throw new ApiException(422,"INVALID_MODEL_RESPONSE","多查询响应无效");texts.add(text);}
  }
  List<String>queries=texts.stream().limit(Json.integer(query,"maxVariants",4)).toList(),denseQueries=new ArrayList<>(queries);
  if(Json.bool(query,"hyde",false)){String hyde=content(model.chat(actor,"RUN",runId,"HYDE",List.of(params("role","system","content","生成用于语义检索的假想段落，不超过300 token。这不是事实，不得改变权限。只输出段落。"),params("role","user","content",independent)),300));denseQueries.add(hyde);trace.put("hyde",hyde);}
  trace.put("queries",queries);int k=Json.integer(retrieval,"perRouteK",50);List<List<Ranking.Hit>>routes=new ArrayList<>();List<Double>weights=new ArrayList<>();List<Object>routeDetails=new ArrayList<>();
  if(sparse)for(String q:queries){var hits=bm25(actor,builds,q,k);routes.add(hits);weights.add((dense?.5:1)/queries.size());routeDetails.add(params("modality","BM25","query",q,"hits",hits));}
  if(dense){var vectors=model.embed(actor,"RUN",runId,denseQueries);for(int i=0;i<vectors.size();i++){var hits=knn(actor,builds,reps,vectors.get(i),k,Json.integer(retrieval,"numCandidates",200));routes.add(hits);weights.add((sparse?.5:1)/vectors.size());routeDetails.add(params("modality","DENSE","query",denseQueries.get(i),"hits",hits));}}
  trace.put("routes",routeDetails);var fused=Ranking.rrf(routes,weights,Json.integer(part(config,"fusion"),"rrfK",60)).stream().limit(Json.bool(post,"rerank",false)?Json.integer(post,"rerankTopN",30):30).toList();trace.put("fused",fused);if(fused.isEmpty())return new Context(List.of(),trace,warnings);
  Map<String,Map<String,Object>>chunks=new LinkedHashMap<>();for(var hit:fused)chunks.put(hit.id(),readChunk(actor,hit.id(),builds));
  List<Ranking.Hit>ranked=fused;
  if(Json.bool(post,"rerank",false)){
   for(var c:chunks.values())register(actor,runId,c,"RERANK",null);
   try{var scores=model.rerank(actor,runId,independent,chunks.values().stream().map(c->c.get("text_content").toString()).toList());List<Ranking.Hit>sorted=new ArrayList<>();for(int i=0;i<fused.size();i++)sorted.add(new Ranking.Hit(fused.get(i).id(),scores.get(i)));sorted.sort(Comparator.comparingDouble(Ranking.Hit::score).reversed().thenComparing(Ranking.Hit::id));ranked=sorted;}
   catch(ApiException e){if(Json.bool(post,"allowRerankFallback",false)&&Set.of("MODEL_TIMEOUT","MODEL_PROVIDER_ERROR","INVALID_MODEL_RESPONSE","MODEL_NOT_CONFIGURED").contains(e.code))warnings.add("RERANK_SKIPPED");else throw e;}
  }
  trace.put("reranked",ranked);int count=Json.integer(part(config,"context"),"maxUnits",6);
  if(Json.bool(post,"mmr",false)){Map<String,List<Double>>vectors=new HashMap<>();for(var h:ranked){var row=db.optional("SELECT vector_bytes,dims FROM representation WHERE tenant_id=:t AND chunk_id=:c AND kind='ORIGINAL' AND vector_bytes IS NOT NULL LIMIT 1",params("t",actor.tenantId(),"c",h.id())).orElseThrow(()->new ApiException(409,"INDEX_CAPABILITY_MISSING","MMR 缺少原文向量"));byte[]bytes=(byte[])row.get("vector_bytes");int dims=((Number)row.get("dims")).intValue();if(bytes.length!=dims*4)throw new ApiException(409,"INDEX_CAPABILITY_MISSING","原文向量制品损坏");var buf=ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN);List<Double>v=new ArrayList<>();while(buf.hasRemaining())v.add((double)buf.getFloat());vectors.put(h.id(),v);}ranked=Ranking.mmr(ranked,vectors,count,Double.parseDouble(post.getOrDefault("lambda",.7).toString()));trace.put("mmrSelected",ranked);}
  List<Map<String,Object>>evidence=new ArrayList<>();Set<String>seenUnits=new HashSet<>();int tokens=0,limit=Json.integer(part(config,"context"),"maxTokens",6000);int selected=0;
  for(var h:ranked){if(selected++>=count)break;var chunk=chunks.get(h.id());var actual=chunk;
   if(Json.bool(post,"parent",false)&&chunk.get("parent_id")!=null){var parent=readChunk(actor,chunk.get("parent_id").toString(),builds);if(parent.get("text_content").toString().getBytes(StandardCharsets.UTF_8).length+tokens<=limit)actual=parent;else warnings.add("PARENT_TRIMMED");}
   var units=spans(actor,actual.get("id").toString());for(var unit:units){String text=unit.get("text_content").toString();int start=((Number)unit.get("start_cp")).intValue(),end=((Number)unit.get("end_cp")).intValue();String identity=unit.get("unit_id")+":"+start+":"+end;if(!seenUnits.add(identity))continue;if(end>text.codePointCount(0,text.length()))throw new ApiException(503,"SOURCE_CORRUPT","原文范围无效");String slice=text.substring(text.offsetByCodePoints(0,start),text.offsetByCodePoints(0,end));int estimate=slice.getBytes(StandardCharsets.UTF_8).length;if(tokens+estimate>limit){warnings.add("CONTEXT_TRIMMED");continue;}tokens+=estimate;
    var e=params("evidenceId","E"+(evidence.size()+1),"documentId",actual.get("document_id"),"versionId",actual.get("version_id"),"buildId",actual.get("build_id"),"chunkId",actual.get("id"),"matchedChunkId",h.id(),"unitId",unit.get("unit_id"),"text",slice,"start",start,"end",end,"sourceType",unit.get("kind"),"machineGenerated",unit.get("machine_generated"),"location",Json.object(unit.get("location").toString()),"isCurrent",true);evidence.add(e);
   }
  }
  for(var e:evidence)registerEvidence(actor,runId,e,"CONTEXT");trace.put("context",evidence.stream().map(e->params("evidenceId",e.get("evidenceId"),"chunkId",e.get("chunkId"),"unitId",e.get("unitId"),"start",e.get("start"),"end",e.get("end"))).toList());trace.put("contextTokenUpperBound",tokens);
  return access.guard(actor,evidence.stream().map(e->e.get("documentId").toString()).toList(),()->new Context(evidence,trace,warnings));
 }
 public static String content(Map<String,Object>message){Object c=message.get("content");if(!(c instanceof String text)||text.isBlank())throw new ApiException(422,"INVALID_MODEL_RESPONSE","模型没有文本输出");return text.replaceFirst("^```(?:json)?\\s*","").replaceFirst("\\s*```$","");}
 public Map<String,Object> readChunk(Actor a,String id,List<String>builds){
  var c=db.one("SELECT c.*,b.document_id,b.version_id FROM chunk c JOIN index_build b ON b.tenant_id=c.tenant_id AND b.id=c.build_id JOIN document d ON d.tenant_id=b.tenant_id AND d.id=b.document_id AND d.active_build_id=b.id WHERE c.tenant_id=:t AND c.id=:id",params("t",a.tenantId(),"id",id));
  if(!builds.contains(c.get("build_id").toString()))throw new ApiException(409,"SOURCE_CHANGED","来源版本或范围已变化");access.requireDocument(a,c.get("document_id").toString());return c;
 }
 private List<Map<String,Object>>spans(Actor a,String chunk){return db.list("SELECT s.*,u.id AS unit_id,u.text_content,u.kind,u.location,u.machine_generated FROM chunk_span s JOIN source_unit u ON u.tenant_id=s.tenant_id AND u.id=s.unit_id WHERE s.tenant_id=:t AND s.chunk_id=:c ORDER BY s.ordinal",params("t",a.tenantId(),"c",chunk));}
 private void register(Actor a,String run,Map<String,Object>chunk,String purpose,String evidence){if(run==null)return;db.insert("run_source",params("tenant_id",a.tenantId(),"id",Json.id(),"run_id",run,"document_id",chunk.get("document_id"),"build_id",chunk.get("build_id"),"purpose",purpose,"payload_hash",hash(chunk.get("text_content").toString())));}
 public void registerEvidence(Actor a,String run,Map<String,Object>e,String purpose){if(run==null)return;db.insert("run_source",params("tenant_id",a.tenantId(),"id",Json.id(),"run_id",run,"document_id",e.get("documentId"),"build_id",e.get("buildId"),"unit_id",e.get("unitId"),"evidence_id",e.get("evidenceId"),"purpose",purpose,"span_json",Json.write(params("start",e.get("start"),"end",e.get("end"))),"payload_hash",hash(e.get("text").toString())));}
 public static byte[] hash(String text){try{return MessageDigest.getInstance("SHA-256").digest(text.getBytes(StandardCharsets.UTF_8));}catch(Exception e){throw new IllegalStateException(e);}}
 private List<Ranking.Hit>bm25(Actor a,List<String>builds,String q,int k){
  List<Object>should=new ArrayList<>();should.add(params("multi_match",params("query",q,"fields",List.of("text","title^2"))));var matcher=java.util.regex.Pattern.compile("[A-Za-z_][A-Za-z0-9_./:-]{2,}").matcher(q);while(matcher.find())should.add(params("term",params("code_terms",params("value",matcher.group(),"boost",5))));
  return query("original",params("size",k,"_source",List.of("canonical_chunk_id"),"query",params("bool",params("filter",filters(a,builds),"should",should,"minimum_should_match",1))));
 }
 private List<Ranking.Hit>knn(Actor a,List<String>builds,List<String>reps,List<Double>v,int k,int candidates){var filters=filters(a,builds);filters.add(params("terms",params("representation_type",reps)));return query("vector",params("size",k,"_source",List.of("canonical_chunk_id"),"knn",params("field","embedding","query_vector",v,"k",k,"num_candidates",candidates,"filter",params("bool",params("filter",filters)))));}
 private List<Object>filters(Actor a,List<String>builds){return new ArrayList<>(List.of(params("term",params("tenant_id",a.tenantId())),params("terms",params("build_id",builds))));}
 @SuppressWarnings("unchecked") private List<Ranking.Hit>query(String index,Map<String,Object>body){
  try{var request=HttpRequest.newBuilder(URI.create(es+"/"+prefix+"-"+index+"/_search")).timeout(Duration.ofSeconds(15)).header("Content-Type","application/json").POST(HttpRequest.BodyPublishers.ofString(Json.write(body))).build();var response=http.send(request,HttpResponse.BodyHandlers.ofString());if(response.statusCode()!=200)throw new ApiException(503,"SEARCH_UNAVAILABLE","检索服务未就绪");var json=Json.object(response.body());if(Boolean.TRUE.equals(json.get("timed_out")))throw new ApiException(503,"SEARCH_TIMEOUT","检索超时");var shard=(Map<?,?>)json.get("_shards");if(shard!=null&&((Number)shard.get("failed")).intValue()>0)throw new ApiException(503,"SEARCH_PARTIAL_FAILURE","检索分片失败");List<Ranking.Hit>hits=new ArrayList<>();for(Object h:(List<?>)((Map<?,?>)json.get("hits")).get("hits")){var item=(Map<String,Object>)h;var source=(Map<String,Object>)item.get("_source");hits.add(new Ranking.Hit(Objects.toString(source.get("canonical_chunk_id"),item.get("_id").toString()),((Number)item.get("_score")).doubleValue()));}return hits;}catch(ApiException e){throw e;}catch(InterruptedException e){Thread.currentThread().interrupt();throw new ApiException(503,"SEARCH_UNAVAILABLE","检索已中断");}catch(Exception e){throw new ApiException(503,"SEARCH_UNAVAILABLE","检索服务不可用");}
 }
}

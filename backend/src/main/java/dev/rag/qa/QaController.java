package dev.rag.qa;
import dev.rag.common.*;
import dev.rag.identity.AccessPolicy;
import dev.rag.search.SearchService;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.web.bind.annotation.*;
import org.springframework.http.ResponseEntity;
import java.util.*;
import static dev.rag.common.Db.params;
@RestController
@RequestMapping("/api/v1")
public class QaController {
 private final AccessPolicy access;private final QaService qa;private final SearchService search;private final Db db;
 public QaController(AccessPolicy access,QaService qa,SearchService search,Db db){this.access=access;this.qa=qa;this.search=search;this.db=db;}
 @PostMapping("/conversations") public Object create(HttpServletRequest request,@RequestBody Map<String,Object>body){return qa.createConversation(access.actor(request),body);}
 @GetMapping("/conversations") public Object list(HttpServletRequest r){return qa.conversations(access.actor(r));}
 @GetMapping("/conversations/{id}") public Object detail(HttpServletRequest r,@PathVariable String id){return qa.conversationDetail(access.actor(r),id);}
 @DeleteMapping("/conversations/{id}") public Object delete(HttpServletRequest r,@PathVariable String id){qa.deleteConversation(access.actor(r),id);return params("status","DELETED");}
 @PostMapping("/conversations/{id}/segments") public Object segment(HttpServletRequest r,@PathVariable String id,@RequestBody Map<String,Object>body){return qa.newSegment(access.actor(r),id,body);}
 @PostMapping("/conversations/{id}/runs") public ResponseEntity<?>submit(HttpServletRequest r,@PathVariable String id,@RequestBody Map<String,Object>body,@RequestHeader(value="Idempotency-Key",required=false)String key){return ResponseEntity.accepted().body(qa.submit(access.actor(r),id,body,key));}
 @GetMapping("/runs/{id}") public Object get(HttpServletRequest r,@PathVariable String id){return qa.get(access.actor(r),id);}
 @PostMapping("/runs/{id}/cancel") public Object cancel(HttpServletRequest r,@PathVariable String id){return qa.cancel(access.actor(r),id);}
 @PutMapping("/runs/{id}/feedback") public Object feedback(HttpServletRequest r,@PathVariable String id,@RequestBody Map<String,Object>body){return qa.feedback(access.actor(r),id,body);}
 @GetMapping("/runs/{id}/evidence/{evidenceId}") public Object evidence(HttpServletRequest r,@PathVariable String id,@PathVariable String evidenceId){var result=qa.get(access.actor(r),id);if(!(result.get("evidence") instanceof List<?>list))throw ApiException.missing();return list.stream().filter(e->Objects.equals(((Map<?,?>)e).get("evidenceId"),evidenceId)).findFirst().orElseThrow(ApiException::missing);}
 @GetMapping("/admin/runs/{id}/trace") public Object trace(HttpServletRequest r,@PathVariable String id){return qa.trace(access.actor(r),id);}
 @GetMapping("/search") public Object search(HttpServletRequest r,@RequestParam String query,@RequestParam(required=false)List<String>kbIds){var a=access.actor(r);return search.search(a,null,query,kbIds==null?List.of():kbIds,Map.of("retrieval",Map.of("mode","BM25")),List.of());}
 @GetMapping("/admin/feedback") public Object feedbackList(HttpServletRequest r){var a=access.actor(r);access.requireAdmin(a);List<Map<String,Object>>out=new ArrayList<>();for(var row:db.list("SELECT * FROM feedback WHERE tenant_id=:t ORDER BY created_at DESC LIMIT 100",params("t",a.tenantId()))){try{qa.trace(a,row.get("run_id").toString());out.add(feedbackView(row));}catch(ApiException e){if(e.status!=404)throw e;out.add(params("id",row.get("id"),"status",row.get("status"),"visibility","HIDDEN"));}}return out;}
 @GetMapping("/admin/feedback/{id}") public Object feedbackDetail(HttpServletRequest r,@PathVariable String id){var a=access.actor(r);access.requireAdmin(a);var row=db.one("SELECT * FROM feedback WHERE tenant_id=:t AND id=:id",params("t",a.tenantId(),"id",id));qa.trace(a,row.get("run_id").toString());return feedbackView(row);}
 @PatchMapping("/admin/feedback/{id}") public Object feedbackUpdate(HttpServletRequest r,@PathVariable String id,@RequestBody Map<String,Object>body){var a=access.actor(r);feedbackDetail(r,id);String status=Json.str(body,"status"),resolution=Json.str(body,"resolution");if(!Set.of("OPEN","IN_PROGRESS","RESOLVED").contains(status)||resolution.length()>4000)throw new ApiException(400,"INVALID_INPUT","处理参数无效");db.exec("UPDATE feedback SET status=:s,resolution_text=:x WHERE tenant_id=:t AND id=:id",params("s",status,"x",resolution,"t",a.tenantId(),"id",id));return params("status",status);}
 private Map<String,Object>feedbackView(Map<String,Object>r){return params("id",r.get("id"),"runId",r.get("run_id"),"rating",r.get("rating"),"issueType",r.get("issue_type"),"comment",r.get("comment_text"),"status",r.get("status"),"resolution",r.get("resolution_text"));}
}

package dev.rag.model;
import dev.rag.common.*;
import dev.rag.identity.AccessPolicy;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.web.bind.annotation.*;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.math.BigDecimal;
import java.util.*;
import static dev.rag.common.Db.params;
@RestController
@RequestMapping("/api/v1")
public class BudgetController {
 private final BudgetService budgets;private final AccessPolicy access;private final String token,tenant,caller;
 public BudgetController(BudgetService budgets,AccessPolicy access,@Value("${rag.internal-budget-token:}")String token,@Value("${rag.internal-budget-tenant:}")String tenant,@Value("${rag.internal-budget-caller:evaluation}")String caller){this.budgets=budgets;this.access=access;this.token=token;this.tenant=tenant;this.caller=caller;}
 @GetMapping("/admin/budget") public Object get(HttpServletRequest r){var a=access.actor(r);access.requireAdmin(a);return budgets.summary(a);}
 private String sign(String id,Map<String,Object>price){try{var mac=javax.crypto.Mac.getInstance("HmacSHA256");mac.init(new javax.crypto.spec.SecretKeySpec(token.getBytes(StandardCharsets.UTF_8),"HmacSHA256"));return Base64.getUrlEncoder().withoutPadding().encodeToString(mac.doFinal((id+Json.write(new TreeMap<>(price))).getBytes(StandardCharsets.UTF_8)));}catch(Exception e){throw new IllegalStateException(e);}}
 private Actor internal(HttpServletRequest r){String header=r.getHeader("Authorization");if(token.isBlank()||tenant.isBlank()||header==null||!MessageDigest.isEqual(("Bearer "+token).getBytes(StandardCharsets.UTF_8),header.getBytes(StandardCharsets.UTF_8))||r.getHeader("Origin")!=null)throw new ApiException(403,"OPERATION_FORBIDDEN","内部服务认证失败");return new Actor(tenant,"",false);}
 @PostMapping("/internal/budget/reservations") public Object reserve(HttpServletRequest r,@RequestBody Map<String,Object>body){var a=internal(r);if(!caller.equals(Json.str(body,"callerEnvironment")))throw new ApiException(403,"OPERATION_FORBIDDEN","未知预算调用方");String id=Json.str(body,"callId");try{UUID.fromString(id);}catch(Exception e){throw new ApiException(400,"INVALID_INPUT","调用 ID 无效");}int in=Json.integer(body,"maxInputTokens",-1),out=Json.integer(body,"maxOutputTokens",-1);if(in<0||in>120000||out<0||out>6000)throw new ApiException(400,"INVALID_INPUT","预留 token 无效");var price=budgets.pricing(a);var row=budgets.reserve(a,caller,id,BudgetService.estimate(price,in,out));return params("reservationId",row.get("id"),"status",row.get("status"),"pricingSnapshot",price,"pricingSignature",sign(row.get("id").toString(),price),"reservedAmount",row.get("reserved_amount"));}
 @PostMapping("/internal/budget/reservations/{id}/settle") public Object settle(HttpServletRequest r,@PathVariable String id,@RequestBody Map<String,Object>body){var a=internal(r);String status=Json.str(body,"status");BigDecimal actual=null;if(status.equals("SETTLED")){int in=Json.integer(body,"inputTokens",-1),out=Json.integer(body,"outputTokens",-1);if(in<0||out<0)throw new ApiException(400,"INVALID_INPUT","结算需要实际用量");var snapshot=body.get("pricingSnapshot") instanceof Map<?,?> ? Json.object(Json.write(body.get("pricingSnapshot"))) : Map.<String,Object>of();String signature=Json.str(body,"pricingSignature");if(!MessageDigest.isEqual(sign(id,snapshot).getBytes(StandardCharsets.UTF_8),signature.getBytes(StandardCharsets.UTF_8)))throw new ApiException(403,"OPERATION_FORBIDDEN","价格快照认证失败");actual=BudgetService.estimate(snapshot,in,out);}budgets.settle(a,id,status,actual);return params("reservationId",id,"status",status);}
}

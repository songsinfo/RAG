package dev.rag.identity;
import dev.rag.common.*;
import jakarta.servlet.http.*;
import org.springframework.web.bind.annotation.*;
import org.springframework.http.ResponseCookie;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import org.springframework.beans.factory.annotation.Value;
import java.security.SecureRandom;
import java.time.*;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;

@RestController @RequestMapping("/api/v1")
public class IdentityController {
 private final Db db;private final AccessPolicy access;private final IdentityService service;private final CsrfProtection csrf;private final boolean secure;
 private final BCryptPasswordEncoder passwords=new BCryptPasswordEncoder(12);
 private final Map<String,List<Long>> attempts=new ConcurrentHashMap<>();
 private final String dummy=new BCryptPasswordEncoder(12).encode("no-such-user");
 public IdentityController(Db db,AccessPolicy access,IdentityService service,CsrfProtection csrf,@Value("${rag.cookie-secure:false}")boolean secure){this.db=db;this.access=access;this.service=service;this.csrf=csrf;this.secure=secure;}
 public static String token(){byte[] b=new byte[32];new SecureRandom().nextBytes(b);return Base64.getUrlEncoder().withoutPadding().encodeToString(b);}
 static void cookie(HttpServletResponse r,String name,String value,boolean httpOnly,boolean secure,long seconds){r.addHeader("Set-Cookie",ResponseCookie.from(name,value).httpOnly(httpOnly).secure(secure).sameSite("Lax").path("/").maxAge(seconds).build().toString());}
 @GetMapping("/auth/csrf") public Map<String,Object> csrf(HttpServletRequest r,HttpServletResponse response){return Map.of("csrfToken",csrf.issue(r,response));}
 @PostMapping("/auth/login") public Map<String,Object> login(@RequestBody Map<String,Object> body,HttpServletRequest r,HttpServletResponse response){String tenant=IdentityService.required(body,"tenantCode",64),login=IdentityService.required(body,"loginName",128),password=IdentityService.required(body,"password",128);String key=r.getRemoteAddr()+":"+tenant+":"+login;long now=System.currentTimeMillis();if(attempts.size()>10000)attempts.entrySet().removeIf(e->e.getValue().stream().allMatch(t->now-t>60000));var bucket=attempts.computeIfAbsent(key,k->new ArrayList<>());synchronized(bucket){bucket.removeIf(t->now-t>60000);if(bucket.size()>=5)throw new ApiException(429,"RATE_LIMITED","登录尝试过于频繁");bucket.add(now);}
  var row=db.optional("SELECT u.* FROM app_user u JOIN tenant t ON t.id=u.tenant_id WHERE t.code=:t AND u.login_name=:u AND u.status='ACTIVE'",Db.params("t",tenant,"u",login));boolean valid=passwords.matches(password,row.map(x->x.get("password_hash").toString()).orElse(dummy));if(row.isEmpty()||!valid)throw new ApiException(401,"INVALID_CREDENTIALS","账号或密码错误");var user=row.get();String token=token();db.insert("auth_session",Db.params("tenant_id",user.get("tenant_id"),"id",Json.id(),"user_id",user.get("id"),"token_hash",AccessPolicy.hash(token),"expires_at",java.sql.Timestamp.from(Instant.now().plusSeconds(28800))));cookie(response,"RAG_SESSION",token,true,secure,28800);cookie(response,"RAG_CSRF","",false,secure,0);return Map.of("authenticated",true);
 }
 @PostMapping("/auth/logout") public Map<String,Object> logout(HttpServletRequest r,HttpServletResponse response){Actor a=access.actor(r);db.exec("UPDATE auth_session SET revoked_at=UTC_TIMESTAMP(6) WHERE tenant_id=:t AND token_hash=:h",Db.params("t",a.tenantId(),"h",AccessPolicy.hash(AccessPolicy.cookie(r,"RAG_SESSION"))));cookie(response,"RAG_SESSION","",true,secure,0);return Map.of("authenticated",false);}
 @GetMapping("/me") public Map<String,Object> me(HttpServletRequest r){return service.me(access.actor(r));}
 @GetMapping("/knowledge-bases") public Object kbs(HttpServletRequest r){return service.knowledgeBases(access.actor(r));}
 @PostMapping("/admin/knowledge-bases") public Object create(HttpServletRequest r,@RequestBody Map<String,Object>b){return service.createKb(access.actor(r),b);}
 @GetMapping("/admin/knowledge-bases/{id}/acl") public Object kbAcl(HttpServletRequest r,@PathVariable String id){return service.acl(access.actor(r),id,false);}
 @PutMapping("/admin/knowledge-bases/{id}/acl") public Object kbAcl(HttpServletRequest r,@PathVariable String id,@RequestBody Map<String,Object>b){return service.setAcl(access.actor(r),id,false,b);}
 @GetMapping("/admin/documents/{id}/acl") public Object docAcl(HttpServletRequest r,@PathVariable String id){return service.acl(access.actor(r),id,true);}
 @PutMapping("/admin/documents/{id}/acl") public Object docAcl(HttpServletRequest r,@PathVariable String id,@RequestBody Map<String,Object>b){return service.setAcl(access.actor(r),id,true,b);}
 @GetMapping("/admin/users") public Object users(HttpServletRequest r){return service.users(access.actor(r));}
 @GetMapping("/admin/groups") public Object groups(HttpServletRequest r){return service.groups(access.actor(r));}
 @PutMapping("/admin/groups/{id}/members") public Object members(HttpServletRequest r,@PathVariable String id,@RequestBody Map<String,Object>b){return service.members(access.actor(r),id,b);}
}

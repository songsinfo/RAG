package dev.rag.search;
import dev.rag.common.*;
import dev.rag.identity.AccessPolicy;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.web.bind.annotation.*;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;
import java.util.*;
import static dev.rag.common.Db.params;
@RestController
@RequestMapping("/api/v1/admin/configs")
public class ConfigController {
 private final Db db;private final AccessPolicy access;private final TransactionTemplate tx;
 public ConfigController(Db db,AccessPolicy access,PlatformTransactionManager manager){this.db=db;this.access=access;this.tx=new TransactionTemplate(manager);}
 @GetMapping public Object list(HttpServletRequest r){var a=access.actor(r);access.requireAdmin(a);return db.list("SELECT id,kind,name,revision,status,payload,created_at AS createdAt FROM config_revision WHERE tenant_id=:t ORDER BY created_at DESC LIMIT 100",params("t",a.tenantId()));}
 @SuppressWarnings("unchecked") @PostMapping public Object create(HttpServletRequest r,@RequestBody Map<String,Object>body){var a=access.actor(r);access.requireAdmin(a);String kind=Json.str(body,"kind"),name=Json.str(body,"name");if(!Set.of("BUILD","RETRIEVAL","MODEL","PROMPT","LIMITS","EVAL").contains(kind)||name.isBlank()||name.length()>128||!(body.get("payload") instanceof Map<?,?>))throw new ApiException(400,"INVALID_CONFIG","配置字段无效");var payload=(Map<String,Object>)body.get("payload");if(kind.equals("RETRIEVAL"))SearchService.validateConfig(payload);String json=Json.write(payload);if(json.length()>64000||json.toLowerCase(Locale.ROOT).contains("api_key")||json.toLowerCase(Locale.ROOT).contains("apikey"))throw new ApiException(400,"INVALID_CONFIG","配置不可包含密钥");return tx.execute(s->{db.one("SELECT id FROM tenant WHERE id=:t FOR UPDATE",params("t",a.tenantId()));int revision=((Number)db.one("SELECT COALESCE(MAX(revision),0)+1 AS n FROM config_revision WHERE tenant_id=:t AND kind=:k AND name=:n",params("t",a.tenantId(),"k",kind,"n",name)).get("n")).intValue();String id=Json.id();db.insert("config_revision",params("tenant_id",a.tenantId(),"id",id,"kind",kind,"name",name,"revision",revision,"sha256",SearchService.hash(json),"payload",json,"status","DRAFT"));return params("id",id,"revision",revision,"status","DRAFT");});}
 @PostMapping("/{id}/publish") public Object publish(HttpServletRequest r,@PathVariable String id){var a=access.actor(r);access.requireAdmin(a);return tx.execute(s->{var config=db.one("SELECT * FROM config_revision WHERE tenant_id=:t AND id=:id FOR UPDATE",params("t",a.tenantId(),"id",id));if(config.get("kind").equals("RETRIEVAL"))SearchService.validateConfig(Json.object(config.get("payload").toString()));db.exec("UPDATE config_revision SET status='PUBLISHED' WHERE tenant_id=:t AND id=:id",params("t",a.tenantId(),"id",id));if(config.get("kind").equals("RETRIEVAL"))db.exec("UPDATE tenant SET active_retrieval_config_id=:id WHERE id=:t",params("t",a.tenantId(),"id",id));db.insert("audit_event",params("tenant_id",a.tenantId(),"id",Json.id(),"actor_id",a.userId(),"action","CONFIG_PUBLISH","object_type","CONFIG","object_id",id,"outcome","SUCCESS"));return params("id",id,"status","PUBLISHED");});}
}

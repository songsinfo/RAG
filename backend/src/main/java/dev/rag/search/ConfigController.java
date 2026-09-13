package dev.rag.search;

import dev.rag.common.*;
import dev.rag.identity.AccessPolicy;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.web.bind.annotation.*;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;

import java.util.*;

import static dev.rag.common.Db.params;

/**
 * 知识库与模型系统配置版本控制 REST 控制器。
 * <p>
 * 支持多类型配置（BUILD, RETRIEVAL, MODEL, PROMPT, LIMITS, EVAL）的不可变多版本生命周期管理（Draft 草稿 -> Published 发布）：
 * <ul>
 *   <li>安全合规审计：入库配置自动扫描明文 API 密钥（如 api_key, apikey），发现即拦截；</li>
 *   <li>强一致版本分配：通过租户级悲观锁保障相同配置名称与类型的 revision 单调自增；</li>
 *   <li>能力兼容性门禁：在发布 RETRIEVAL 检索配置前，校验租户活跃索引能力是否能够支撑该配置（如混合检索需依赖向量与全文倒排）。</li>
 * </ul>
 */
@RestController
@RequestMapping("/api/v1/admin/configs")
public class ConfigController {
    private final Db db;
    private final AccessPolicy access;
    private final TransactionTemplate tx;
    private final SearchService search;

    /**
     * 构造配置控制器。
     *
     * @param db 统一数据库操作组件
     * @param access 访问控制组件
     * @param manager 事务管理器
     * @param search 检索业务服务（用于配置兼容性校验）
     */
    public ConfigController(Db db, AccessPolicy access, PlatformTransactionManager manager, SearchService search) {
        this.db = db;
        this.access = access;
        this.search = search;
        this.tx = new TransactionTemplate(manager);
    }

    /**
     * 管理员查询租户所有的配置修订历史记录（最多 100 条）。
     *
     * @param r HTTP 请求对象
     * @return 配置修订记录列表（包含 id, kind, name, revision, status, payload, createdAt）
     */
    @GetMapping
    public Object list(HttpServletRequest r) {
        var a = access.actor(r);
        access.requireAdmin(a);
        return db.list("SELECT id,kind,name,revision,status,payload,created_at AS createdAt FROM config_revision WHERE tenant_id=:t ORDER BY created_at DESC LIMIT 100", params("t", a.tenantId()));
    }

    /**
     * 创建新的配置修订版本草稿（DRAFT）。
     * <p>
     * <b>校验与并发控制逻辑：</b>
     * <ul>
     *   <li>配置类型校验：必须属于 BUILD / RETRIEVAL / MODEL / PROMPT / LIMITS / EVAL；</li>
     *   <li>敏感词阻断：payload JSON 长度上限 64,000 字符，且禁止包含 "api_key" 或 "apikey"，防止密钥意外持久化；</li>
     *   <li>类型特化校验：若为 RETRIEVAL 配置，委托 {@link SearchService#validateConfig} 校验分块与检索参数区间；</li>
     *   <li>事务内行锁：锁定 tenant 行，查询 (kind, name) 的当前最大 revision，原子自增并插入状态为 DRAFT 的记录。</li>
     * </ul>
     *
     * @param r HTTP 请求对象
     * @param body 请求负载（包含 kind, name, payload）
     * @return 包含新生成的 id, revision 与状态 DRAFT 的 Map
     * @throws ApiException 400（参数缺失或包含密钥敏感词）
     */
    @SuppressWarnings("unchecked")
    @PostMapping
    public Object create(HttpServletRequest r, @RequestBody Map<String, Object> body) {
        var a = access.actor(r);
        access.requireAdmin(a);
        String kind = Json.str(body, "kind"), name = Json.str(body, "name");
        if (!Set.of("BUILD", "RETRIEVAL", "MODEL", "PROMPT", "LIMITS", "EVAL").contains(kind) || name.isBlank() || name.length() > 128 || !(body.get("payload") instanceof Map<?, ?>))
            throw new ApiException(400, "INVALID_CONFIG", "配置字段无效");
        var payload = (Map<String, Object>) body.get("payload");
        if (kind.equals("RETRIEVAL")) SearchService.validateConfig(payload);
        String json = Json.write(payload);
        if (json.length() > 64000 || json.toLowerCase(Locale.ROOT).contains("api_key") || json.toLowerCase(Locale.ROOT).contains("apikey"))
            throw new ApiException(400, "INVALID_CONFIG", "配置不可包含密钥");
        return tx.execute(s -> {
            db.one("SELECT id FROM tenant WHERE id=:t FOR UPDATE", params("t", a.tenantId()));
            int revision = ((Number) db.one("SELECT COALESCE(MAX(revision),0)+1 AS n FROM config_revision WHERE tenant_id=:t AND kind=:k AND name=:n", params("t", a.tenantId(), "k", kind, "n", name)).get("n")).intValue();
            String id = Json.id();
            db.insert("config_revision", params("tenant_id", a.tenantId(), "id", id, "kind", kind, "name", name, "revision", revision, "sha256", SearchService.hash(json), "payload", json, "status", "DRAFT"));
            return params("id", id, "revision", revision, "status", "DRAFT");
        });
    }

    /**
     * 将指定配置草稿正式发布生效（PUBLISHED）。
     * <p>
     * <b>发布流水线：</b>
     * <ul>
     *   <li>悲观锁锁定目标配置修订记录；</li>
     *   <li>能力适配门禁：若为 RETRIEVAL 配置，调用 {@link SearchService#validateCapabilities} 校验现有文档构建能力是否满足需求；</li>
     *   <li>更新配置状态为 PUBLISHED；</li>
     *   <li>若为 RETRIEVAL 配置，同步将租户的 active_retrieval_config_id 指向该配置 ID；</li>
     *   <li>记录 CONFIG_PUBLISH 审计事件。</li>
     * </ul>
     *
     * @param r HTTP 请求对象
     * @param id 待发布的配置修订 ID
     * @return 包含 id 与状态 PUBLISHED 的 Map
     */
    @PostMapping("/{id}/publish")
    public Object publish(HttpServletRequest r, @PathVariable String id) {
        var a = access.actor(r);
        access.requireAdmin(a);
        return tx.execute(s -> {
            var config = db.one("SELECT * FROM config_revision WHERE tenant_id=:t AND id=:id FOR UPDATE", params("t", a.tenantId(), "id", id));
            if (config.get("kind").equals("RETRIEVAL"))
                search.validateCapabilities(a, Json.object(config.get("payload").toString()));
            db.exec("UPDATE config_revision SET status='PUBLISHED' WHERE tenant_id=:t AND id=:id", params("t", a.tenantId(), "id", id));
            if (config.get("kind").equals("RETRIEVAL"))
                db.exec("UPDATE tenant SET active_retrieval_config_id=:id WHERE id=:t", params("t", a.tenantId(), "id", id));
            db.insert("audit_event", params("tenant_id", a.tenantId(), "id", Json.id(), "actor_id", a.userId(), "action", "CONFIG_PUBLISH", "object_type", "CONFIG", "object_id", id, "outcome", "SUCCESS"));
            return params("id", id, "status", "PUBLISHED");
        });
    }
}

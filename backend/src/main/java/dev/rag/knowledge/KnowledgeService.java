package dev.rag.knowledge;

import dev.rag.common.*;
import dev.rag.identity.*;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

import java.util.*;
import java.nio.charset.StandardCharsets;
import java.sql.Timestamp;
import java.time.Instant;

/**
 * 知识库文档管理与后台索引任务服务。
 * <p>
 * 核心职责包含：
 * <ul>
 *   <li>文档上传与多版本归档：支持幂等键去重、内容 SHA-256 查重及私有对象存储管理；</li>
 *   <li>构建配置校验：标准化分块 codepoint 大小与多模态/增强索引配置；</li>
 *   <li>异步任务调度：负责摄取（INGEST）、清理（PURGE）等后台 job 的入队与状态跟踪；</li>
 *   <li>文档生命周期流转：基于行版本号（row_version）和生命周期代际屏障（lifecycle_epoch）管理 TRASH/RESTORE/PURGE 状态机；</li>
 *   <li>任务控制与质量审核：支持任务重试/取消，以及针对包含质量警告的构建执行人工放行（NEEDS_REVIEW -> CHUNKING）。</li>
 * </ul>
 */
@Service
public class KnowledgeService {
    final Db db;
    final AccessPolicy access;
    final IdentityService identity;
    final TransactionTemplate tx;
    final PrivateArtifacts files;

    /**
     * 构造知识库核心服务。
     *
     * @param db 统一数据库操作组件
     * @param access 访问控制与权限校验策略
     * @param identity 身份认证、审计与租户版本服务
     * @param tx 事务管理模板
     * @param files 私有文件与构件存储管理组件
     */
    public KnowledgeService(Db db, AccessPolicy access, IdentityService identity, TransactionTemplate tx, PrivateArtifacts files) {
        this.db = db;
        this.access = access;
        this.identity = identity;
        this.tx = tx;
        this.files = files;
    }

    /**
     * 上传或更新文档版本并触发后台异步摄取索引流水线。
     * <p>
     * <b>执行步骤与安全/可靠性机制：</b>
     * <ol>
     *   <li>权限与入参校验：验证调用者具备管理员权限；校验幂等键非空且不超过 80 字符；校验文件名与字节流；校验知识库及现有文档所有权。</li>
     *   <li>请求级幂等控制：将租户用户与 idempotency_key 组合，结合入参及文件 SHA-256 计算请求哈希。
     *       若已存在相同 key 的 INGEST 任务，比对请求哈希：一致则直接复用既有任务结果；不一致则抛出 409 IDEMPOTENCY_CONFLICT。</li>
     *   <li>文件落盘与异常补偿：先将源文件写入私有存储目录；若后续数据库事务失败，在 catch 中主动物理删除已保存文件，避免孤儿文件泄漏。</li>
     *   <li>事务内并发与去重：
     *       <ul>
     *         <li>悲观锁：获取文档行锁（SELECT ... FOR UPDATE），保证版本号分配与生命周期状态校验并发安全；</li>
     *         <li>内容级去重：查询是否存在完全相同文件内容哈希与构建配置哈希的历史版本。若已存在，删除刚写入的存储文件并复用原有构建任务；</li>
     *         <li>多版本记录生成：生成 artifact、document_version（自增 version_no）、config_revision 及 index_build 记录；</li>
     *         <li>排队异步任务：将任务写入 job 表，初始阶段为 PARSING，状态为 QUEUED，带上当前文档的 lifecycle_epoch 作为并发屏障；</li>
     *         <li>记录审计日志：写入 DOCUMENT_UPLOAD 审计事件。</li>
     *       </ul>
     *   </li>
     * </ol>
     *
     * @param a 操作用户主体
     * @param kb 目标知识库 ID
     * @param documentId 文档 ID，新建文档传 null，追加新版本传既有文档 ID
     * @param name 上传的文件原始名称
     * @param bytes 文件字节数组
     * @param key 客户端幂等性请求键（Idempotency-Key）
     * @param config 摄取构建自定义配置（如 childCodePoints、embedding 等）
     * @return 包含 documentId, versionId, buildId, jobId, status 的任务元数据 Map
     * @throws ApiException 若权限不足、入参不合规、文档不存在或幂等性冲突
     */
    public Map<String, Object> upload(Actor a, String kb, String documentId, String name, byte[] bytes, String key, Map<String, Object> config) {
        access.requireAdmin(a);
        if (key == null || key.isBlank() || key.length() > 80)
            throw IdentityService.invalid("需要最多80字符的 Idempotency-Key");
        InputValidation.validate(bytes, name);
        if (!access.canReadKb(a, kb)) throw ApiException.missing();
        if (documentId != null) access.requireDocument(a, documentId);
        byte[] requestHash = AccessPolicy.hash((kb + ":" + documentId + ":" + name + ":" + HexFormat.of().formatHex(AccessPolicy.hash(bytes)) + ":" + Json.write(config)).getBytes(StandardCharsets.UTF_8));
        String scoped = a.userId() + ":" + key;
        var existing = db.optional("SELECT * FROM job WHERE tenant_id=:t AND kind='INGEST' AND idempotency_key=:k", Db.params("t", a.tenantId(), "k", scoped));
        if (existing.isPresent()) {
            if (!Arrays.equals((byte[]) existing.get().get("request_hash"), requestHash))
                throw new ApiException(409, "IDEMPOTENCY_CONFLICT", "相同键对应不同上传");
            return jobResult(a, existing.get());
        }
        String storage = files.save(a.tenantId(), bytes);
        try {
            return access.write(a, () -> tx.execute(s -> {
                String doc = documentId;
                if (doc == null) {
                    doc = Json.id();
                    db.insert("document", Db.params("tenant_id", a.tenantId(), "id", doc, "kb_id", kb, "title", name));
                    db.insert("document_grant", Db.params("tenant_id", a.tenantId(), "id", Json.id(), "document_id", doc, "principal_type", "USER", "user_id", a.userId()));
                }
                var current = db.one("SELECT * FROM document WHERE tenant_id=:t AND id=:d FOR UPDATE", Db.params("t", a.tenantId(), "d", doc));
                if (!current.get("status").equals("LIVE") || !current.get("kb_id").equals(kb))
                    throw ApiException.missing();
                String payload = Json.write(validateConfig(config));
                byte[] configHash = AccessPolicy.hash(payload);
                var duplicate = db.optional("SELECT j.* FROM document_version v JOIN index_build b ON b.tenant_id=v.tenant_id AND b.version_id=v.id JOIN config_revision c ON c.tenant_id=b.tenant_id AND c.id=b.config_id JOIN job j ON j.tenant_id=b.tenant_id AND j.target_id=b.id AND j.kind='INGEST' WHERE v.tenant_id=:t AND v.document_id=:d AND v.source_hash=:h AND c.sha256=:ch ORDER BY v.version_no DESC LIMIT 1", Db.params("t", a.tenantId(), "d", doc, "h", AccessPolicy.hash(bytes), "ch", configHash));
                if (duplicate.isPresent()) {
                    files.delete(storage);
                    return jobResult(a, duplicate.get());
                }
                String artifact = Json.id(), version = Json.id(), build = Json.id(), configId = Json.id();
                int number = ((Number) db.one("SELECT COALESCE(MAX(version_no),0)+1 AS n FROM document_version WHERE tenant_id=:t AND document_id=:d", Db.params("t", a.tenantId(), "d", doc)).get("n")).intValue();
                db.insert("artifact", Db.params("tenant_id", a.tenantId(), "id", artifact, "document_id", doc, "kind", "SOURCE", "storage_key", storage, "sha256", AccessPolicy.hash(bytes), "size_bytes", bytes.length));
                db.insert("document_version", Db.params("tenant_id", a.tenantId(), "id", version, "document_id", doc, "version_no", number, "source_artifact_id", artifact, "source_hash", AccessPolicy.hash(bytes), "original_name", name, "media_type", "application/octet-stream", "created_by", a.userId()));
                db.insert("config_revision", Db.params("tenant_id", a.tenantId(), "id", configId, "kind", "BUILD", "name", build, "revision", 1, "sha256", configHash, "payload", payload, "status", "PUBLISHED"));
                db.insert("index_build", Db.params("tenant_id", a.tenantId(), "id", build, "document_id", doc, "version_id", version, "build_no", 1, "config_id", configId, "status", "QUEUED", "capabilities", "[]", "fence", current.get("lifecycle_epoch")));
                String job = queue(a, "INGEST", build, scoped, requestHash, current.get("lifecycle_epoch"), "PARSING");
                identity.audit(a, "DOCUMENT_UPLOAD", "DOCUMENT", doc);
                return Db.params("documentId", doc, "versionId", version, "buildId", build, "jobId", job, "status", "QUEUED");
            }));
        } catch (RuntimeException e) {
            files.delete(storage);
            throw e;
        }
    }

    /**
     * 校验并标准化索引构建与分块配置。
     * <p>
     * 规则约束：
     * <ul>
     *   <li>子块字符数（childCodePoints）：必须在 [64, 4000] 区间内，默认 900；</li>
     *   <li>父块字符数（parentCodePoints）：必须在 [childCodePoints, 12000] 区间内，默认 3000；</li>
     *   <li>解析特性开关：支持 embedding（向量化）、summary（摘要）、questions（问答对扩展）、vision（多模态视觉）；</li>
     *   <li>分词器版本固定为 "unicode-codepoint-estimate-v1"。</li>
     * </ul>
     *
     * @param c 客户端传入的原始构建配置 Map
     * @return 标准化后的不可变配置 Map
     * @throws ApiException 若分块长度配置超出合规区间
     */
    static Map<String, Object> validateConfig(Map<String, Object> c) {
        boolean embedding = Json.bool(c, "embedding", false), summary = Json.bool(c, "summary", false), questions = Json.bool(c, "questions", false);
        int child = Json.integer(c, "childCodePoints", 900), parent = Json.integer(c, "parentCodePoints", 3000);
        if (child < 64 || child > 4000 || parent < child || parent > 12000)
            throw IdentityService.invalid("分块长度无效");
        return Map.of("embedding", embedding, "summary", summary, "questions", questions, "vision", Json.bool(c, "vision", false), "childCodePoints", child, "parentCodePoints", parent, "tokenizer", "unicode-codepoint-estimate-v1");
    }

    /**
     * 向异步任务作业表（job）注册并排队新任务。
     *
     * @param a 操作主体
     * @param kind 任务类型（如 INGEST, PURGE, RESTORE 等）
     * @param target 任务目标实体 ID（如 buildId 或 documentId）
     * @param key 幂等控制键
     * @param hash 请求哈希指纹，防重入篡改
     * @param fence 任务对应的生命周期代际屏障（lifecycle_epoch），防止过期任务生效
     * @param stage 任务初始执行阶段（如 PARSING, PURGE 等）
     * @return 新生成的任务作业唯一标识 ID
     */
    public String queue(Actor a, String kind, String target, String key, byte[] hash, Object fence, String stage) {
        String id = Json.id();
        db.insert("job", Db.params("tenant_id", a.tenantId(), "id", id, "kind", kind, "target_id", target, "requested_by", a.userId(), "idempotency_key", key, "request_hash", hash, "status", "QUEUED", "stage", stage, "available_at", Timestamp.from(Instant.now()), "fence", fence, "checkpoint", "{}"));
        return id;
    }

    /**
     * 根据 job 记录及关联的 index_build 组装统一任务状态响应。
     *
     * @param a 操作主体
     * @param j 从 job 表查询出的任务行记录 Map
     * @return 包含 documentId, versionId, buildId, jobId, status 的任务元数据 Map
     */
    private Map<String, Object> jobResult(Actor a, Map<String, Object> j) {
        var b = db.one("SELECT document_id,version_id FROM index_build WHERE tenant_id=:t AND id=:b", Db.params("t", a.tenantId(), "b", j.get("target_id")));
        return Db.params("documentId", b.get("document_id"), "versionId", b.get("version_id"), "buildId", j.get("target_id"), "jobId", j.get("id"), "status", j.get("status"));
    }

    /**
     * 分页查询文档列表，根据用户角色与细粒度权限动态脱敏。
     * <p>
     * <b>权限与过滤逻辑：</b>
     * <ul>
     *   <li>管理员模式（admin=true）：校验管理员权限，不过滤私有 ACL 授权，仅根据租户与状态过滤；</li>
     *   <li>普通用户模式（admin=false）：应用 {@link AccessPolicy#readable()} 行级 SQL 授权过滤；</li>
     *   <li>敏感数据脱敏：若调用者最终不具备某文档的显式读取权限，则自动擦除其 title、activeBuildId 和 activeVersionId 字段。</li>
     * </ul>
     *
     * @param a 查询用户主体
     * @param kb 可选的知识库 ID 过滤条件
     * @param admin 是否以管理员视角全量查询
     * @param status 文档状态过滤条件，默认查询 LIVE
     * @return 文档元数据记录列表（最多 100 条）
     */
    public List<Map<String, Object>> list(Actor a, String kb, boolean admin, String status) {
        if (admin) access.requireAdmin(a);
        var params = Db.params("t", a.tenantId(), "u", a.userId(), "kb", kb, "status", status == null ? "LIVE" : status);
        String where = admin ? "d.tenant_id=:t AND d.status=:status" : AccessPolicy.readable();
        if (kb != null && !kb.isBlank()) where += " AND d.kb_id=:kb";
        var rows = db.list("SELECT d.id,d.kb_id AS kbId,d.title,d.status,d.row_version AS rowVersion,d.active_build_id AS activeBuildId,(SELECT version_id FROM index_build b WHERE b.tenant_id=d.tenant_id AND b.id=d.active_build_id) AS activeVersionId FROM document d WHERE " + where + " ORDER BY d.created_at DESC LIMIT 100", params);
        for (var r : rows)
            if (!access.canReadDocument(a, r.get("id").toString())) {
                r.remove("title");
                r.remove("activeBuildId");
                r.remove("activeVersionId");
            }
        return rows;
    }

    /**
     * 获取单个文档的基础元数据。
     * <p>
     * 采用 {@link AccessPolicy#guard} 进行鉴权保护，确保调用方对该文档具备合法访问权限。
     *
     * @param a 操作主体
     * @param id 文档唯一标识 ID
     * @return 文档详情 Map（含 kbId, title, status, rowVersion, activeBuildId 等）
     */
    public Map<String, Object> get(Actor a, String id) {
        return access.guard(a, List.of(id), () -> db.one("SELECT id,kb_id AS kbId,title,status,row_version AS rowVersion,active_build_id AS activeBuildId FROM document WHERE tenant_id=:t AND id=:d", Db.params("t", a.tenantId(), "d", id)));
    }

    /**
     * 预览指定文档版本的索引构建结果与解析单元列表。
     * <p>
     * 返回对应构建的质量告警列表（quality_warnings）以及按顺序排列的解析源单元（source_unit），
     * 供前端进行构建质量复核或解析内容抽检。
     *
     * @param a 操作主体
     * @param doc 文档唯一标识 ID
     * @param version 目标文档版本 ID
     * @return 包含构建状态、质量告警和前 100 个文本单元片段的记录列表
     */
    public Object preview(Actor a, String doc, String version) {
        return access.guard(a, List.of(doc), () -> {
            var v = db.one("SELECT id FROM document_version WHERE tenant_id=:t AND document_id=:d AND id=:v", Db.params("t", a.tenantId(), "d", doc, "v", version));
            return db.list("SELECT b.id AS buildId,b.status,b.quality_warnings AS warnings,u.id AS unitId,u.kind,u.text_content AS text,u.location,u.machine_generated AS machineGenerated,u.artifact_id AS artifactId FROM index_build b LEFT JOIN source_unit u ON u.tenant_id=b.tenant_id AND u.build_id=b.id WHERE b.tenant_id=:t AND b.document_id=:d AND b.version_id=:v ORDER BY b.build_no DESC,u.unit_key LIMIT 100", Db.params("t", a.tenantId(), "d", doc, "v", version));
        });
    }

    /**
     * 变更文档生命周期状态（移入回收站 trash / 恢复 restore / 彻底清除 purge）。
     * <p>
     * <b>核心并发与一致性保障机制：</b>
     * <ul>
     *   <li><b>悲观行锁与状态机合法性检查：</b>使用 SELECT ... FOR UPDATE 锁定文档行，并校验前置状态：
     *       <ul>
     *         <li>trash 仅允许从 LIVE 状态流转；</li>
     *         <li>restore 仅允许从 TRASHED 状态流转；</li>
     *         <li>purge 仅允许从 TRASHED 状态流转。</li>
     *       </ul>
     *   </li>
     *   <li><b>乐观版本锁（Optimistic Locking）：</b>对比传入的 expected 版本号与数据库 row_version，不匹配则抛出 409 冲突；</li>
     *   <li><b>生命周期纪元屏障（Lifecycle Epoch Fence）：</b>递增 lifecycle_epoch。
     *       此屏障可立即使后台正在执行的旧纪元构建或解析任务失效，防止废弃构建覆盖最新文档状态；</li>
     *   <li><b>清理记录追踪：</b>对于彻底清除（PURGE），生成 deletion_record 审计记录并设置 7 天备份宽限期（backup_expiry_at），
     *       同时排队 PURGE 后台异步物理清理任务；</li>
     *   <li><b>租户代际递增：</b>触发 {@link IdentityService#epoch}，使所有已缓存的租户权限或状态快照失效。</li>
     * </ul>
     *
     * @param a 操作主体（必须具备管理员权限）
     * @param doc 文档唯一标识 ID
     * @param action 生命周期操作动作（trash / restore / purge）
     * @param expected 客户端期望的文档行版本号（用于乐观锁防覆盖）
     * @return 包含 documentId, status 及可选 jobId 的结果 Map
     * @throws ApiException 若状态不合法或行版本冲突
     */
    public Map<String, Object> lifecycle(Actor a, String doc, String action, long expected) {
        access.requireAdmin(a);
        return access.write(a, () -> tx.execute(s -> {
            var d = db.one("SELECT * FROM document WHERE tenant_id=:t AND id=:d FOR UPDATE", Db.params("t", a.tenantId(), "d", doc));
            if (((Number) d.get("row_version")).longValue() != expected) throw IdentityService.conflict();
            String state = d.get("status").toString();
            if (action.equals("trash") && !state.equals("LIVE") || action.equals("restore") && !state.equals("TRASHED") || action.equals("purge") && !state.equals("TRASHED"))
                throw new ApiException(409, "INVALID_STATE", "文档状态不允许此操作");
            long fence = ((Number) d.get("lifecycle_epoch")).longValue() + 1;
            String status = action.equals("trash") ? "TRASHED" : action.equals("purge") ? "PURGE_PENDING" : "TRASHED";
            db.exec("UPDATE document SET status=:s,lifecycle_epoch=:f,row_version=row_version+1,deleted_at=COALESCE(deleted_at,UTC_TIMESTAMP(6)) WHERE tenant_id=:t AND id=:d", Db.params("s", status, "f", fence, "t", a.tenantId(), "d", doc));
            identity.epoch(a);
            identity.audit(a, "DOCUMENT_" + action.toUpperCase(Locale.ROOT), "DOCUMENT", doc);
            if (action.equals("trash")) return Map.of("documentId", doc, "status", status);
            String kind = action.toUpperCase(Locale.ROOT);
            String job = queue(a, kind, doc, Json.id(), AccessPolicy.hash(doc + fence), fence, kind);
            if (kind.equals("PURGE"))
                db.insert("deletion_record", Db.params("tenant_id", a.tenantId(), "id", Json.id(), "document_id", doc, "origin_system", "local", "requested_by", a.userId(), "status", "PENDING", "domains_json", "{}", "backup_expiry_at", Timestamp.from(Instant.now().plusSeconds(7 * 86400))));
            return Map.of("documentId", doc, "status", status, "jobId", job);
        }));
    }

    /**
     * 查询租户后台作业任务列表或指定任务详情。
     *
     * @param a 操作主体（要求管理员权限）
     * @param id 可选的任务唯一标识 ID；若为 null 则查询最近 100 条任务
     * @return 任务列表或单条任务记录
     */
    public Object jobs(Actor a, String id) {
        access.requireAdmin(a);
        return db.list("SELECT id,kind,target_id AS targetId,status,stage,attempt,error_code AS errorCode,lease_epoch AS leaseEpoch FROM job WHERE tenant_id=:t" + (id == null ? "" : " AND id=:id") + " ORDER BY created_at DESC LIMIT 100", Db.params("t", a.tenantId(), "id", id));
    }

    /**
     * 对后台作业执行管控操作（取消 cancel 或重试 retry）。
     * <p>
     * <b>状态控制逻辑：</b>
     * <ul>
     *   <li>取消操作（cancel=true）：若任务尚未到达终态（非 COMPLETED/FAILED/CANCELLED），将其置为 CANCELLED，
     *       记录 cancel_requested_at 并递增 lease_epoch 强行使当前持有着租约的工作线程失效；</li>
     *   <li>重试操作（cancel=false）：必须处于 FAILED 或 CANCELLED 终态；
     *       若之前因解析超时失败（PARSER_TIMEOUT），会自动重置检查点中的 externalTaskId 和超时起始标记；
     *       将任务重置为 QUEUED，重试计数清零，递增 lease_epoch 允许重新抢占执行。</li>
     * </ul>
     *
     * @param a 操作主体（要求管理员权限）
     * @param id 目标任务 ID
     * @param cancel true 表示取消任务，false 表示重试任务
     * @return 更新后的最新任务状态列表
     * @throws ApiException 若重试时任务所处状态不合法
     */
    public Object controlJob(Actor a, String id, boolean cancel) {
        access.requireAdmin(a);
        return access.write(a, () -> tx.execute(s -> {
            var j = db.one("SELECT * FROM job WHERE tenant_id=:t AND id=:id FOR UPDATE", Db.params("t", a.tenantId(), "id", id));
            String state = j.get("status").toString();
            if (cancel) {
                if (!Set.of("COMPLETED", "FAILED", "CANCELLED").contains(state))
                    db.exec("UPDATE job SET status='CANCELLED',cancel_requested_at=UTC_TIMESTAMP(6),lease_epoch=lease_epoch+1 WHERE tenant_id=:t AND id=:id", Db.params("t", a.tenantId(), "id", id));
            } else {
                if (!Set.of("FAILED", "CANCELLED").contains(state))
                    throw new ApiException(409, "INVALID_STATE", "任务当前不可重试");
                var cp = Json.object(Objects.toString(j.get("checkpoint"), "{}"));
                if ("PARSER_TIMEOUT".equals(j.get("error_code"))) {
                    cp.remove("externalTaskId");
                    cp.remove(ParserDeadline.START);
                }
                db.exec("UPDATE job SET status='QUEUED',cancel_requested_at=NULL,error_code=NULL,attempt=0,checkpoint=:cp,available_at=UTC_TIMESTAMP(6),lease_epoch=lease_epoch+1 WHERE tenant_id=:t AND id=:id", Db.params("t", a.tenantId(), "id", id, "cp", Json.write(cp)));
            }
            identity.audit(a, cancel ? "JOB_CANCEL" : "JOB_RETRY", "JOB", id);
            return jobs(a, id);
        }));
    }

    /**
     * 人工审核确认构建质量警告并推进流水线。
     * <p>
     * 当索引构建流水线检测到文本质量告警（如格式异常、大段截断等）时会暂停并进入 NEEDS_REVIEW 状态。
     * 管理员人工复核后提交审核原因（review_reason），本方法将构建状态推进为 CHUNKING，
     * 并同步将关联的 INGEST 任务重置为 QUEUED/CHUNKING 状态重新唤醒后台调度。
     *
     * @param a 操作主体（要求管理员权限）
     * @param build 待确认的 index_build 唯一标识 ID
     * @param body 请求体，必须包含 "reason" 审核确认说明字段（最长 2000 字符）
     * @return 包含 buildId 与状态 QUEUED 的响应 Map
     * @throws ApiException 若当前构建不处于 NEEDS_REVIEW 状态
     */
    public Object accept(Actor a, String build, Map<String, Object> body) {
        access.requireAdmin(a);
        String reason = IdentityService.required(body, "reason", 2000);
        var b = db.one("SELECT document_id,status FROM index_build WHERE tenant_id=:t AND id=:b", Db.params("t", a.tenantId(), "b", build));
        return access.guard(a, List.of(b.get("document_id").toString()), () -> tx.execute(s -> {
            if (!b.get("status").equals("NEEDS_REVIEW"))
                throw new ApiException(409, "INVALID_STATE", "构建不在待审查状态");
            db.exec("UPDATE index_build SET review_reason=:r,status='CHUNKING' WHERE tenant_id=:t AND id=:b", Db.params("r", reason, "t", a.tenantId(), "b", build));
            db.exec("UPDATE job SET status='QUEUED',stage='CHUNKING',available_at=UTC_TIMESTAMP(6) WHERE tenant_id=:t AND target_id=:b AND kind='INGEST' AND status='NEEDS_REVIEW'", Db.params("t", a.tenantId(), "b", build));
            identity.audit(a, "WARNINGS_ACCEPTED", "BUILD", build);
            return Map.of("buildId", build, "status", "QUEUED");
        }));
    }
}

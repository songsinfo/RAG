package dev.rag.knowledge;

import dev.rag.common.*;
import dev.rag.identity.*;
import org.springframework.stereotype.Service;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.transaction.support.TransactionTemplate;
import org.springframework.web.bind.annotation.*;
import jakarta.servlet.http.HttpServletRequest;

import java.net.*;
import java.net.http.*;
import java.time.Duration;
import java.util.*;
import java.security.MessageDigest;
import java.nio.charset.StandardCharsets;

/**
 * 文档删除级联脱敏与集群副本同步传播服务。
 * <p>
 * <b>隐私合规与数据擦除保证：</b>
 * 1. 物理级联清理：同步删除向量索引、物理存储工件（PDF/图片/分块）；
 * 2. 溯源衍生数据脱敏（Scrubbing）：清空相关历史问答会话运行记录（qa_run）、引用来源、评分评测（eval_result）及分块向量；
 * 3. 集群节点对等传播：跨环境/多节点通知，确保评测快照与副本实例同步永久清除（PURGED）。
 */
@Service
public class DeletionPropagation {
    private final Db db;
    private final AccessPolicy access;
    private final TransactionTemplate tx;
    private final PrivateArtifacts files;
    private final IndexWriter index;
    private final String peers, token, environment;

    /**
     * 构造 DeletionPropagation 实例。
     *
     * @param db          数据库访问组件
     * @param access      访问策略组件
     * @param tx          事务模板
     * @param files       文件工件存储组件
     * @param index       索引写入组件
     * @param peers       集群对等节点地址列表（逗号分隔）
     * @param token       内部对等调用通信 Bearer Token
     * @param environment 当前系统运行环境标识
     */
    public DeletionPropagation(Db db, AccessPolicy access, TransactionTemplate tx, PrivateArtifacts files, IndexWriter index, @Value("${RAG_DELETION_PEERS:}") String peers, @Value("${RAG_DELETION_API_TOKEN:}") String token, @Value("${rag.environment:demo}") String environment) {
        this.db = db;
        this.access = access;
        this.tx = tx;
        this.files = files;
        this.index = index;
        this.peers = peers;
        this.token = token;
        this.environment = environment;
    }

    /**
     * 向本地及所有对等节点同步广播文档彻底删除动作。
     * 仅调用运维预置的固定节点地址，严禁使用用户或文档提供的外部 URL。
     *
     * @param tenant 所属租户 ID
     * @param doc    目标文档 ID
     * @throws ApiException 若未配置对等凭据或对等节点未确认清理成功
     */
    public void propagate(String tenant, String doc) {
        receive(environment, tenant, doc);
        if (peers.isBlank()) return;
        if (token.length() < 32) throw new ApiException(503, "DELETION_PEER_NOT_CONFIGURED", "副本清理凭据未配置");
        for (String peer : peers.split(",")) {
            try {
                URI uri = URI.create(peer.strip() + "/api/v1/internal/deletions");
                if (!Set.of("http", "https").contains(uri.getScheme())) throw new IllegalArgumentException();
                var request = HttpRequest.newBuilder(uri).timeout(Duration.ofSeconds(20)).header("Authorization", "Bearer " + token).header("Content-Type", "application/json").POST(HttpRequest.BodyPublishers.ofString(Json.write(Map.of("originSystem", environment, "originTenantId", tenant, "originDocumentId", doc)))).build();
                var r = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build().send(request, HttpResponse.BodyHandlers.ofString());
                if (r.statusCode() != 200 || !Boolean.TRUE.equals(Json.object(r.body()).get("acknowledged")))
                    throw new ApiException(503, "DELETION_PEER_PENDING", "副本实例尚未确认清理");
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new ApiException(503, "DELETION_PEER_PENDING", "副本清理中断");
            } catch (java.io.IOException | IllegalArgumentException e) {
                throw new ApiException(503, "DELETION_PEER_PENDING", "副本实例清理失败");
            }
        }
    }

    /**
     * 内部集群删除 API 的请求鉴权。
     * 验证预共享 Token（常数时间比对防止时序攻击），禁止携带浏览器 Origin 头。
     *
     * @param request 当前 HTTP 请求
     * @throws ApiException 若令牌不匹配或包含跨域 Origin 头（403 OPERATION_FORBIDDEN）
     */
    public void authenticate(HttpServletRequest request) {
        String supplied = request.getHeader("Authorization");
        if (token.length() < 32 || request.getHeader("Origin") != null || supplied == null || !MessageDigest.isEqual(("Bearer " + token).getBytes(StandardCharsets.UTF_8), supplied.getBytes(StandardCharsets.UTF_8)))
            throw new ApiException(403, "OPERATION_FORBIDDEN", "内部服务身份无效");
    }

    /**
     * 接收并执行删除动作。
     * 级联清理评测快照副本、物理文件、向量全文索引及评测黄金标注集。
     *
     * @param origin 来源系统环境标识
     * @param tenant 来源租户 ID
     * @param doc    来源文档 ID
     * @return 执行确认结果 Map {"acknowledged": true, "copiesPurged": count}
     */
    public Map<String, Object> receive(String origin, String tenant, String doc) {
        if (origin.isBlank() || !tenant.matches("[A-Za-z0-9-]{1,36}") || !doc.matches("[A-Za-z0-9-]{1,36}"))
            throw IdentityService.invalid("删除来源无效");
        var p = Db.params("o", origin, "t", tenant, "d", doc);
        String sourceMatch = origin.equals(environment) ? "(origin_system=:o OR origin_system='LOCAL_SYNTHETIC')" : "origin_system=:o";
        var copies = db.list("SELECT DISTINCT tenant_id,document_id FROM eval_snapshot_document WHERE " + sourceMatch + " AND origin_tenant_id=:t AND origin_document_id=:d", p);
        for (var copy : copies) {
            String t = copy.get("tenant_id").toString(), d = copy.get("document_id").toString();
            access.write(new Actor(t, "", false), () -> {
                tx.executeWithoutResult(s -> db.exec("UPDATE document SET status='PURGE_PENDING',lifecycle_epoch=lifecycle_epoch+1,active_build_id=NULL WHERE tenant_id=:t AND id=:d", Db.params("t", t, "d", d)));
                index.delete(t, d);
                for (var f : db.list("SELECT storage_key FROM artifact WHERE tenant_id=:t AND document_id=:d", Db.params("t", t, "d", d)))
                    files.delete(f.get("storage_key").toString());
                tx.executeWithoutResult(s -> {
                    scrubContent(t, d);
                    db.exec("UPDATE document SET status='PURGED',title=NULL WHERE tenant_id=:t AND id=:d", Db.params("t", t, "d", d));
                    db.exec("UPDATE eval_snapshot SET status='PURGED',manifest_json=JSON_OBJECT() WHERE tenant_id=:t AND id IN (SELECT snapshot_id FROM eval_snapshot_document WHERE tenant_id=:t AND document_id=:d)", Db.params("t", t, "d", d));
                });
                return null;
            });
        }
        tx.executeWithoutResult(s -> {
            var golds = db.list("SELECT DISTINCT tenant_id,case_id FROM eval_gold WHERE " + sourceMatch + " AND origin_tenant_id=:t AND origin_document_id=:d", p);
            for (var g : golds) {
                var gp = Db.params("t", g.get("tenant_id"), "c", g.get("case_id"));
                db.exec("UPDATE eval_case SET question_json=JSON_OBJECT(),expected_json=JSON_OBJECT(),annotation_status='PURGED' WHERE tenant_id=:t AND id=:c", gp);
                db.exec("UPDATE eval_gold SET anchor_json=JSON_OBJECT() WHERE tenant_id=:t AND case_id=:c", gp);
                db.exec("UPDATE eval_dataset_version SET status='PURGED' WHERE tenant_id=:t AND id IN (SELECT dataset_version_id FROM eval_case WHERE tenant_id=:t AND id=:c)", gp);
                db.exec("UPDATE eval_review SET labels_json=JSON_OBJECT(),reason_text=NULL WHERE tenant_id=:t AND result_id IN (SELECT id FROM eval_result WHERE tenant_id=:t AND case_id=:c)", gp);
                db.exec("UPDATE eval_result SET metrics_json=NULL,judge_json=NULL,status='PURGED' WHERE tenant_id=:t AND case_id=:c", gp);
            }
        });
        return Map.of("acknowledged", true, "copiesPurged", copies.size());
    }

    /**
     * 深度擦除指定文档衍生出的全部文本内容、问答记录与向量表征。
     * <p>
     * <b>数据脱敏范围：</b>
     * 1. 取消并脱敏所有曾引用该文档的 QA 运行实例（qa_run、run_source、run_event）；
     * 2. 清空分块文本（chunk.text_content=''）并重设哈希；
     * 3. 清空多模态表征（representation.vector_bytes=NULL）；
     * 4. 清除原始解析单元文本（source_unit）与工件记录。
     *
     * @param tenant 租户 ID
     * @param doc    待脱敏擦除的文档 ID
     */
    public void scrubContent(String tenant, String doc) {
        var p = Db.params("t", tenant, "d", doc);
        var affected = db.list("SELECT DISTINCT q.id,q.conversation_id FROM qa_run q JOIN qa_run origin ON origin.tenant_id=q.tenant_id AND origin.segment_id=q.segment_id AND q.turn_no>=origin.turn_no JOIN run_source rs ON rs.tenant_id=origin.tenant_id AND rs.run_id=origin.id WHERE rs.tenant_id=:t AND rs.document_id=:d", p);
        for (var r : affected) {
            var rp = Db.params("t", tenant, "r", r.get("id"), "c", r.get("conversation_id"));
            db.exec("UPDATE qa_run SET question=NULL,result_json=NULL,status='CANCELLED',terminal_reason='SOURCE_PURGED' WHERE tenant_id=:t AND id=:r", rp);
            db.exec("UPDATE conversation SET title=NULL WHERE tenant_id=:t AND id=:c", rp);
            db.exec("UPDATE run_event SET payload=NULL WHERE tenant_id=:t AND run_id=:r", rp);
            db.exec("UPDATE run_source SET span_json=NULL WHERE tenant_id=:t AND run_id=:r", rp);
            db.exec("UPDATE feedback SET comment_text=NULL,resolution_text=NULL WHERE tenant_id=:t AND run_id=:r", rp);
            db.exec("UPDATE eval_review SET labels_json=JSON_OBJECT(),reason_text=NULL WHERE tenant_id=:t AND result_id IN (SELECT id FROM eval_result WHERE tenant_id=:t AND qa_run_id=:r)", rp);
            db.exec("UPDATE eval_result SET metrics_json=NULL,judge_json=NULL,status='PURGED' WHERE tenant_id=:t AND qa_run_id=:r", rp);
        }
        db.exec("UPDATE source_unit SET text_content=NULL,location=JSON_OBJECT() WHERE tenant_id=:t AND document_id=:d", p);
        var cp = new LinkedHashMap<>(p);
        cp.put("h", AccessPolicy.hash(""));
        db.exec("UPDATE chunk SET text_content='',content_hash=:h WHERE tenant_id=:t AND build_id IN (SELECT id FROM index_build WHERE tenant_id=:t AND document_id=:d)", cp);
        db.exec("UPDATE representation SET text_content='',vector_bytes=NULL,dims=NULL,vector_hash=NULL,text_hash=:h WHERE tenant_id=:t AND build_id IN (SELECT id FROM index_build WHERE tenant_id=:t AND document_id=:d)", cp);
        db.exec("UPDATE artifact SET status='PURGED' WHERE tenant_id=:t AND document_id=:d", p);
        db.exec("UPDATE document_version SET original_name=NULL WHERE tenant_id=:t AND document_id=:d", p);
        db.exec("UPDATE index_build SET quality_warnings=NULL,review_reason=NULL WHERE tenant_id=:t AND document_id=:d", p);
    }
}

/**
 * 内部跨节点删除同步控制 API。
 */
@RestController
class DeletionController {
    private final DeletionPropagation deletion;

    DeletionController(DeletionPropagation deletion) {
        this.deletion = deletion;
    }

    /**
     * 接收对等节点派发的删除通知。
     *
     * @param request 当前 HTTP 请求（用于 Bearer Token 验证）
     * @param body    包含 originSystem, originTenantId, originDocumentId 的请求体
     * @return 确认响应 Map
     */
    @PostMapping("/api/v1/internal/deletions")
    Object receive(HttpServletRequest request, @RequestBody Map<String, Object> body) {
        deletion.authenticate(request);
        return deletion.receive(Json.str(body, "originSystem"), Json.str(body, "originTenantId"), Json.str(body, "originDocumentId"));
    }
}

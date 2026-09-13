package dev.rag.evaluation;

import dev.rag.common.*;
import dev.rag.identity.AccessPolicy;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.*;

import static dev.rag.common.Db.params;

/**
 * 离线评测资产目录与题集版本管理服务。
 * <p>
 * 负责评测数据闭环的核心资产生命周期管理：
 * <ul>
 *   <li>题集版本控制：题集从草稿（DRAFT）到不可变冻结（FROZEN）的版本状态流转；</li>
 *   <li>评测用例管理：支持 DEV/TEST 分区划分、用户画像（Persona）、期望状态（ANSWER/PARTIAL/ABSTAIN/CONFLICT）与金标准（Gold Evidence）标注；</li>
 *   <li>防泄漏与引文锚点校验：严禁同组题目跨 DEV/TEST 分区（SPLIT_LEAKAGE），冻结前严格核验金标准引文在 source_unit 原文中的匹配性；</li>
 *   <li>资料环境快照（Snapshot）：锁定用于基准评测的文档构建版本、文件摘要哈希与权限安全纪元（Security Epoch），确保评测可复现性。</li>
 * </ul>
 */
@Service
public class EvaluationCatalog {
    final Db db;
    final AccessPolicy access;
    final TransactionTemplate tx;
    final String environment;

    /**
     * 构造评测资产服务并注入必要组件及运行环境配置。
     *
     * @param db 数据库客户端
     * @param access 权限控制策略
     * @param tx 事务模板
     * @param environment 当前应用环境（要求 evaluation 或 test）
     */
    public EvaluationCatalog(Db db, AccessPolicy access, TransactionTemplate tx, @Value("$" + "{rag.environment:demo}") String environment) {
        this.db = db;
        this.access = access;
        this.tx = tx;
        this.environment = environment;
    }

    /**
     * 校验当前调用主体是否具有管理员权限，并强制要求必须在独立的评测/测试环境中运行。
     *
     * @param a 操作主体
     * @throws ApiException 若不是管理员或当前环境为非评测环境
     */
    void admin(Actor a) {
        access.requireAdmin(a);
        if (!Set.of("evaluation", "test").contains(environment))
            throw new ApiException(409, "EVALUATION_ENVIRONMENT_REQUIRED", "请在独立 evaluation 实例运行评估，不能使用工作库");
    }

    /**
     * 根据主键 ID 在指定表中查询单条记录。
     *
     * @param a 操作主体
     * @param table 目标表名
     * @param id 记录主键 ID
     * @return 记录 Map
     */
    Map<String, Object> row(Actor a, String table, String id) {
        return db.one("SELECT * FROM " + table + " WHERE tenant_id=:t AND id=:id", params("t", a.tenantId(), "id", id));
    }

    /**
     * 将对象安全转换或反序列化为键值对映射（Map）。
     *
     * @param v 任意输入对象
     * @return 转换后的 Map
     */
    static Map<String, Object> obj(Object v) {
        return v == null ? new LinkedHashMap<>() : v instanceof Map<?, ?> ? Json.object(Json.write(v)) : Json.object(v instanceof byte[] b ? new String(b, StandardCharsets.UTF_8) : v.toString());
    }

    /**
     * 将输入列表对象安全转换映射为对象列表（List of Map）。
     *
     * @param v 输入对象
     * @return Map 列表
     * @throws ApiException 若输入非列表格式
     */
    static List<Map<String, Object>> maps(Object v) {
        if (v == null) return List.of();
        if (!(v instanceof List<?> list)) throw new ApiException(400, "INVALID_INPUT", "需要对象数组");
        return list.stream().map(EvaluationCatalog::obj).toList();
    }

    /**
     * 计算输入对象的 SHA-256 散列摘要字节数组。
     *
     * @param v 待计算哈希的对象
     * @return 32 字节哈希数组
     */
    static byte[] hash(Object v) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(Json.write(v).getBytes(StandardCharsets.UTF_8));
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    /**
     * 提取 Map 中非空的指定字符串属性，若缺失或为空则抛出 400 异常。
     *
     * @param b 输入 Map
     * @param key 属性键名
     * @return 去除首尾空白后的属性值
     */
    static String need(Map<String, Object> b, String key) {
        String v = Json.str(b, key).strip();
        if (v.isEmpty()) throw new ApiException(400, "INVALID_INPUT", key + " 必填");
        return v;
    }

    /**
     * 将题集版本数据库记录转换为统一的对外展示视图 Map。
     *
     * @param r 数据库行记录 Map
     * @return 格式化后的题集版本 Map
     */
    static Map<String, Object> datasetView(Map<String, Object> r) {
        return params("id", r.get("id"), "name", r.get("name"), "revision", r.get("revision"), "status", r.get("status"), "frozenAt", Objects.toString(r.get("frozen_at"), null));
    }

    /**
     * 查询当前租户下全部评测题集版本列表（按创建时间降序）。
     *
     * @param a 操作主体（需管理员权限）
     * @return 题集版本列表
     */
    public List<Map<String, Object>> datasets(Actor a) {
        admin(a);
        return db.list("SELECT * FROM eval_dataset_version WHERE tenant_id=:t ORDER BY created_at DESC", params("t", a.tenantId())).stream().map(EvaluationCatalog::datasetView).toList();
    }

    /**
     * 创建新的评测题集版本（初始化为 DRAFT 草稿状态，修订号 revision 自动自增）。
     *
     * @param a 操作主体
     * @param b 请求体 Map（含 name）
     * @return 新建题集版本视图 Map
     */
    public Map<String, Object> createDataset(Actor a, Map<String, Object> b) {
        admin(a);
        String name = need(b, "name");
        return tx.execute(s -> {
            db.one("SELECT id FROM tenant WHERE id=:t FOR UPDATE", params("t", a.tenantId()));
            var n = db.one("SELECT COALESCE(MAX(revision),0)+1 AS rev FROM eval_dataset_version WHERE tenant_id=:t AND name=:n", params("t", a.tenantId(), "n", name));
            String id = Json.id();
            db.insert("eval_dataset_version", params("tenant_id", a.tenantId(), "id", id, "name", name, "revision", ((Number) n.get("rev")).intValue(), "status", "DRAFT"));
            return datasetView(row(a, "eval_dataset_version", id));
        });
    }

    /**
     * 基于已有题集版本深拷贝创建新的题集版本（克隆全部评测题目与金标准事实标注）。
     *
     * @param a 操作主体
     * @param source 源题集版本 ID
     * @return 新创建的题集版本视图 Map
     */
    public Map<String, Object> copy(Actor a, String source) {
        admin(a);
        var original = row(a, "eval_dataset_version", source);
        return tx.execute(s -> {
            var target = createDataset(a, Map.of("name", original.get("name")));
            var cs = cases(a, source);
            if (!cs.isEmpty()) save(a, target.get("id").toString(), cs);
            return target;
        });
    }

    /**
     * 悲观加锁校验指定题集版本是否仍处于草稿（DRAFT）状态；若已冻结则禁止修改。
     *
     * @param a 操作主体
     * @param id 题集版本 ID
     * @throws ApiException 若题集已冻结（409 DATASET_FROZEN）
     */
    void draft(Actor a, String id) {
        var r = db.one("SELECT status FROM eval_dataset_version WHERE tenant_id=:t AND id=:id FOR UPDATE", params("t", a.tenantId(), "id", id));
        if (!"DRAFT".equals(r.get("status")))
            throw new ApiException(409, "DATASET_FROZEN", "冻结题集必须创建新版本再修改");
    }

    /**
     * 将评测用例数据库记录转换为对外统一视图 Map。
     *
     * @param r 数据库行记录 Map
     * @return 格式化后的用例 Map
     */
    static Map<String, Object> caseView(Map<String, Object> r) {
        return params("id", r.get("id"), "caseKey", r.get("case_key"), "groupKey", r.get("group_key"), "split", r.get("split"), "category", r.get("category"), "question", obj(r.get("question_json")), "persona", obj(r.get("persona_json")), "expected", obj(r.get("expected_json")), "annotationStatus", r.get("annotation_status"));
    }

    /**
     * 校验评测用例所引用的所有金标准文档是否属于当前租户且具备读取权限。
     *
     * @param a 操作主体
     * @param c 用例 Map
     */
    void requireCase(Actor a, Map<String, Object> c) {
        var expected = c.containsKey("expected_json") ? obj(c.get("expected_json")) : obj(c.get("expected"));
        for (var group : maps(expected.get("evidenceGroups")))
            for (var anchor : maps(group.get("alternatives"))) access.requireDocument(a, need(anchor, "documentId"));
    }

    /**
     * 查询指定题集版本下的全部评测用例列表，并逐一校验来源文档可见性。
     *
     * @param a 操作主体
     * @param version 题集版本 ID
     * @return 评测用例列表
     */
    public List<Map<String, Object>> cases(Actor a, String version) {
        admin(a);
        row(a, "eval_dataset_version", version);
        return db.list("SELECT * FROM eval_case WHERE tenant_id=:t AND dataset_version_id=:d ORDER BY case_key", params("t", a.tenantId(), "d", version)).stream().map(c -> {
            requireCase(a, c);
            return caseView(c);
        }).toList();
    }

    /**
     * 批量导入或更新评测题集中的用例数据及其金标准标注。
     * <p>
     * <b>校验与落库机制：</b>
     * <ul>
     *   <li>批次规模限制：单次批量提交数量必须在 1 到 1000 题之间；</li>
     *   <li>数据集状态保护：严格加锁校验题集必须处于 DRAFT 草稿状态；</li>
     *   <li>字段格式校验：校验 split（DEV/TEST）、persona（登录身份）、expected 状态（ANSWER/PARTIAL/ABSTAIN/CONFLICT）等；</li>
     *   <li>幂等覆盖：按 caseKey 匹配已有用例，存在则级联清理旧金标准并重新写入；</li>
     *   <li>金标准入库：将证据事实组（evidenceGroups）解析入库为 eval_gold 记录，并记录来源文档版本哈希及相关度评级（0~3）。</li>
     * </ul>
     *
     * @param a 操作主体
     * @param version 目标题集版本 ID
     * @param inputs 待保存的用例列表
     * @return 处理结果摘要 Map（count, datasetVersionId）
     */
    public Map<String, Object> save(Actor a, String version, List<Map<String, Object>> inputs) {
        admin(a);
        if (inputs.isEmpty() || inputs.size() > 1000)
            throw new ApiException(400, "INVALID_INPUT", "一次需包含1到1000题");
        return tx.execute(s -> {
            draft(a, version);
            for (var b : inputs) {
                String key = need(b, "caseKey"), split = need(b, "split").toUpperCase(Locale.ROOT);
                if (!Set.of("DEV", "TEST").contains(split))
                    throw new ApiException(400, "INVALID_INPUT", "split 必须 DEV/TEST");
                var question = b.get("question") instanceof String t ? params("text", t) : obj(b.get("question"));
                if (Json.str(question, "text").isBlank() && maps(question.get("turns")).isEmpty())
                    throw new ApiException(400, "INVALID_INPUT", "问题不能为空");
                var persona = obj(b.get("persona"));
                need(persona, "loginName");
                var expected = obj(b.get("expected"));
                if (!Set.of("ANSWER", "PARTIAL", "ABSTAIN", "CONFLICT").contains(need(expected, "status")))
                    throw new ApiException(400, "INVALID_INPUT", "预期状态无效");
                String annotation = Json.str(b, "annotationStatus");
                if (annotation.isEmpty()) annotation = "DRAFT";
                if (!Set.of("DRAFT", "AUTO_REVIEWED", "HUMAN_REVIEWED").contains(annotation))
                    throw new ApiException(400, "INVALID_INPUT", "标注状态无效");
                requireCase(a, Map.of("expected", expected));
                var old = db.optional("SELECT id FROM eval_case WHERE tenant_id=:t AND dataset_version_id=:d AND case_key=:k", params("t", a.tenantId(), "d", version, "k", key));
                String id = old.map(v -> v.get("id").toString()).orElseGet(Json::id);
                if (old.isPresent()) {
                    db.exec("DELETE FROM eval_gold WHERE tenant_id=:t AND case_id=:id", params("t", a.tenantId(), "id", id));
                    db.exec("DELETE FROM eval_case WHERE tenant_id=:t AND id=:id", params("t", a.tenantId(), "id", id));
                }
                db.insert("eval_case", params("tenant_id", a.tenantId(), "id", id, "dataset_version_id", version, "case_key", key, "split", split, "group_key", need(b, "groupKey"), "category", need(b, "category"), "question_json", Json.write(question), "persona_json", Json.write(persona), "expected_json", Json.write(expected), "annotation_status", annotation));
                for (var group : maps(expected.get("evidenceGroups"))) {
                    int alternative = 0;
                    String fact = need(group, "fact");
                    for (var anchor : maps(group.get("alternatives"))) {
                        String doc = need(anchor, "documentId"), v = need(anchor, "versionId");
                        need(anchor, "quote");
                        var source = db.one("SELECT source_hash FROM document_version WHERE tenant_id=:t AND document_id=:d AND id=:v", params("t", a.tenantId(), "d", doc, "v", v));
                        int grade = Json.integer(anchor, "relevanceGrade", 3);
                        if (grade < 0 || grade > 3) throw new ApiException(400, "INVALID_INPUT", "相关等级必须0..3");
                        db.insert("eval_gold", params("tenant_id", a.tenantId(), "id", Json.id(), "case_id", id, "fact_group", fact, "alternative_no", alternative++, "origin_system", environment, "origin_tenant_id", a.tenantId(), "origin_document_id", doc, "version_hash", source.get("source_hash"), "anchor_json", Json.write(anchor), "relevance_grade", grade));
                    }
                }
            }
            return Map.of("count", inputs.size(), "datasetVersionId", version);
        });
    }

    /**
     * 更新单个评测用例的数据。
     *
     * @param a 操作主体
     * @param id 用例 ID
     * @param b 待修改的字段 Map
     * @return 保存摘要 Map
     */
    public Map<String, Object> update(Actor a, String id, Map<String, Object> b) {
        admin(a);
        var c = row(a, "eval_case", id);
        var merged = caseView(c);
        merged.putAll(b);
        merged.put("caseKey", c.get("case_key"));
        return save(a, c.get("dataset_version_id").toString(), List.of(merged));
    }

    /**
     * 删除指定的单个评测用例及其关联金标准记录（仅在草稿版本下允许）。
     *
     * @param a 操作主体
     * @param id 用例 ID
     */
    public void delete(Actor a, String id) {
        admin(a);
        tx.executeWithoutResult(s -> {
            var c = row(a, "eval_case", id);
            draft(a, c.get("dataset_version_id").toString());
            db.exec("DELETE FROM eval_gold WHERE tenant_id=:t AND case_id=:c", params("t", a.tenantId(), "c", id));
            db.exec("DELETE FROM eval_case WHERE tenant_id=:t AND id=:c", params("t", a.tenantId(), "c", id));
        });
    }

    /**
     * 将题集版本冻结为不可变基准（FROZEN）。
     * <p>
     * <b>冻结前执行严格一致性核验：</b>
     * <ul>
     *   <li>空集校验：题集不能为空；</li>
     *   <li>标注完备性：所有用例的 annotationStatus 不能为 DRAFT；</li>
     *   <li>分区防泄漏（SPLIT_LEAKAGE）：同一 groupKey 的多轮追问/近重复题严禁跨 DEV 与 TEST 分区；</li>
     *   <li>金标准引文定位：对于可回答题目，通过 {@link #validateAnchor} 核验引文必须在目标版本 source_unit 中完全匹配；</li>
     *   <li>固化清单哈希：计算全量用例列表的 SHA-256 摘要保存到 manifest_hash。</li>
     * </ul>
     *
     * @param a 操作主体
     * @param id 题集版本 ID
     * @return 冻结后的题集版本视图 Map
     */
    public Map<String, Object> freeze(Actor a, String id) {
        admin(a);
        return tx.execute(s -> {
            draft(a, id);
            var cs = cases(a, id);
            if (cs.isEmpty()) throw new ApiException(409, "EMPTY_DATASET", "空题集不能冻结");
            Map<String, String> splits = new HashMap<>();
            for (var c : cs) {
                if ("DRAFT".equals(c.get("annotationStatus")))
                    throw new ApiException(409, "ANNOTATION_REQUIRED", "需要完成标注核查");
                String group = c.get("groupKey").toString(), split = c.get("split").toString();
                String old = splits.putIfAbsent(group, split);
                if (old != null && !old.equals(split))
                    throw new ApiException(409, "SPLIT_LEAKAGE", "同组追问/近重复题不能跨开发与测试分区");
                var expected = obj(c.get("expected"));
                var gold = maps(expected.get("evidenceGroups"));
                if (!"ABSTAIN".equals(expected.get("status")) && gold.isEmpty())
                    throw new ApiException(409, "GOLD_REQUIRED", "可答题需要原文事实组");
                for (var g : gold) {
                    if (maps(g.get("alternatives")).isEmpty())
                        throw new ApiException(409, "GOLD_REQUIRED", "事实组缺原文");
                    for (var anchor : maps(g.get("alternatives"))) validateAnchor(a, anchor);
                }
            }
            db.exec("UPDATE eval_dataset_version SET status='FROZEN',manifest_hash=:h,frozen_at=CURRENT_TIMESTAMP(6) WHERE tenant_id=:t AND id=:id", params("t", a.tenantId(), "id", id, "h", hash(cs)));
            return datasetView(row(a, "eval_dataset_version", id));
        });
    }

    /**
     * 校验金标准引文切片（quote）在指定文档版本的 source_unit 中能否精确定位。
     *
     * @param a 操作主体
     * @param anchor 金标准引文锚点 Map
     * @throws ApiException 若找不到匹配文本（409 ANCHOR_UNRESOLVED）
     */
    void validateAnchor(Actor a, Map<String, Object> anchor) {
        access.requireDocument(a, need(anchor, "documentId"));
        var units = db.list("SELECT u.text_content FROM source_unit u JOIN index_build b ON b.tenant_id=u.tenant_id AND b.id=u.build_id WHERE b.tenant_id=:t AND b.document_id=:d AND b.version_id=:v", params("t", a.tenantId(), "d", need(anchor, "documentId"), "v", need(anchor, "versionId")));
        String quote = need(anchor, "quote");
        if (units.stream().noneMatch(u -> Objects.toString(u.get("text_content"), "").contains(quote)))
            throw new ApiException(409, "ANCHOR_UNRESOLVED", "原文版本无法定位金标准引文");
    }

    /**
     * 为选定的知识库文档创建评测运行快照（Snapshot）。
     * <p>
     * 锁定文档的活跃构建版本（active_build_id）、源文件摘要（source_hash）以及租户安全权限纪元（security_epoch），
     * 确保后续评测运行所基于的基础事实数据不可变。
     *
     * @param a 操作主体
     * @param body 请求体 Map（含 name, documentIds, approvedSyntheticOrRedacted）
     * @return 快照详情 Map
     */
    public Map<String, Object> snapshot(Actor a, Map<String, Object> body) {
        admin(a);
        String name = need(body, "name");
        Object raw = body.get("documentIds");
        if (!(raw instanceof List<?> list) || list.isEmpty())
            throw new ApiException(400, "INVALID_INPUT", "必须选择资料");
        List<String> ids = list.stream().map(Object::toString).distinct().toList();
        if (!(Json.bool(body, "approvedSyntheticOrRedacted", false) || Set.of("SYNTHETIC", "REDACTED").contains(Json.str(body, "approval"))))
            throw new ApiException(400, "SOURCE_APPROVAL_REQUIRED", "必须确认资料为模拟或已脱敏");
        return access.guard(a, ids, () -> tx.execute(s -> {
            String id = Json.id();
            List<Map<String, Object>> docs = new ArrayList<>();
            for (String doc : ids) {
                var d = db.one("SELECT d.id,d.active_build_id,b.version_id,b.config_id,v.source_hash FROM document d JOIN index_build b ON b.tenant_id=d.tenant_id AND b.id=d.active_build_id JOIN document_version v ON v.tenant_id=b.tenant_id AND v.id=b.version_id WHERE d.tenant_id=:t AND d.id=:d AND d.status='LIVE'", params("t", a.tenantId(), "d", doc));
                docs.add(params("documentId", doc, "buildId", d.get("active_build_id"), "versionId", d.get("version_id"), "sourceHash", HexFormat.of().formatHex((byte[]) d.get("source_hash")), "buildConfigId", d.get("config_id")));
            }
            var manifest = params("documents", docs, "environment", environment, "securityEpoch", db.one("SELECT security_epoch FROM tenant WHERE id=:t", params("t", a.tenantId())).get("security_epoch"));
            db.insert("eval_snapshot", params("tenant_id", a.tenantId(), "id", id, "name", name, "manifest_hash", hash(manifest), "manifest_json", Json.write(manifest), "status", "READY", "environment_id", environment));
            for (var d : docs)
                db.insert("eval_snapshot_document", params("tenant_id", a.tenantId(), "id", Json.id(), "snapshot_id", id, "document_id", d.get("documentId"), "version_id", d.get("versionId"), "origin_system", environment, "origin_tenant_id", a.tenantId(), "origin_document_id", d.get("documentId"), "origin_version_hash", HexFormat.of().parseHex(d.get("sourceHash").toString())));
            return Map.of("id", id, "snapshotId", id, "manifest", manifest);
        }));
    }

    /**
     * 查询当前租户下的所有评测资料快照列表。
     *
     * @param a 操作主体
     * @return 评测快照摘要列表
     */
    public List<Map<String, Object>> snapshots(Actor a) {
        admin(a);
        return db.list("SELECT * FROM eval_snapshot WHERE tenant_id=:t ORDER BY created_at DESC", params("t", a.tenantId())).stream().map(s -> params("id", s.get("id"), "name", s.get("name"), "status", s.get("status"), "environmentId", s.get("environment_id"), "manifest", obj(s.get("manifest_json")))).toList();
    }

    /**
     * 校验指定评测快照的状态与时效性，提取包含的文档 ID 列表。
     *
     * @param a 操作主体
     * @param id 快照 ID
     * @param checkCurrent 是否核验当前系统的构建版本与权限纪元是否发生变化
     * @return 快照包含的文档 ID 列表
     * @throws ApiException 若快照失效、文档版本漂移或权限纪元改变
     */
    List<String> checkSnapshot(Actor a, String id, boolean checkCurrent) {
        var snap = row(a, "eval_snapshot", id);
        if (!"READY".equals(snap.get("status"))) throw new ApiException(409, "SNAPSHOT_UNAVAILABLE", "快照已失效");
        var manifest = obj(snap.get("manifest_json"));
        List<String> ids = new ArrayList<>();
        if (checkCurrent && !Objects.toString(manifest.get("securityEpoch")).equals(db.one("SELECT security_epoch FROM tenant WHERE id=:t", params("t", a.tenantId())).get("security_epoch").toString()))
            throw new ApiException(409, "ACL_SNAPSHOT_CHANGED", "权限已改变，请新建快照");
        for (var d : maps(manifest.get("documents"))) {
            String doc = d.get("documentId").toString();
            access.requireDocument(a, doc);
            if (checkCurrent && !Objects.equals(row(a, "document", doc).get("active_build_id"), d.get("buildId")))
                throw new ApiException(409, "SOURCE_CHANGED", "生效构建已改变，请新建快照");
            ids.add(doc);
        }
        return ids;
    }
}

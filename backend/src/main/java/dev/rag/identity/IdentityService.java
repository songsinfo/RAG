package dev.rag.identity;

import dev.rag.common.*;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;

import java.util.*;

/**
 * 租户身份体系、组织架构与 ACL 权限管理服务。
 * 负责用户查询、用户组管理、知识库及文档级访问控制列表（ACL）配置与安全版本递增（Epoch）。
 */
@Service
public class IdentityService {
    private final Db db;
    private final AccessPolicy access;
    private final TransactionTemplate tx;

    /**
     * 构造 IdentityService 实例。
     *
     * @param db     数据库访问组件
     * @param access 访问控制策略组件
     * @param tx     事务模板
     */
    public IdentityService(Db db, AccessPolicy access, TransactionTemplate tx) {
        this.db = db;
        this.access = access;
        this.tx = tx;
    }

    /**
     * 记录安全审计事件。
     *
     * @param a      当前操作主体
     * @param action 操作动作类型（如 KB_CREATE、ACL_UPDATE 等）
     * @param type   目标对象类别（如 KB、DOCUMENT、GROUP）
     * @param id     目标对象唯一标识
     */
    public void audit(Actor a, String action, String type, String id) {
        db.insert("audit_event", Db.params("tenant_id", a.tenantId(), "id", Json.id(), "actor_id", a.userId(), "action", action, "object_type", type, "object_id", id, "outcome", "SUCCESS", "metadata_json", "{}"));
    }

    /**
     * 递增租户的安全纪元计数（security_epoch）。
     * 当权限、成员关系或 ACL 发生变更时调用，使依赖缓存的旧鉴权状态失效。
     *
     * @param a 当前操作主体
     */
    public void epoch(Actor a) {
        db.exec("UPDATE tenant SET security_epoch=security_epoch+1 WHERE id=:t", Map.of("t", a.tenantId()));
    }

    /**
     * 查询租户下的所有用户列表。
     * 仅限租户管理员调用。
     *
     * @param a 当前操作主体
     * @return 用户信息列表（含 id, loginName, admin, status）
     * @throws ApiException 若调用方非管理员
     */
    public List<Map<String, Object>> users(Actor a) {
        access.requireAdmin(a);
        return db.list("SELECT id,login_name AS loginName,is_admin AS admin,status FROM app_user WHERE tenant_id=:t", Map.of("t", a.tenantId()));
    }

    /**
     * 查询租户下的所有用户组及组内成员 ID 列表。
     * 仅限租户管理员调用。
     *
     * @param a 当前操作主体
     * @return 用户组信息列表（含 group ID, name, securityEpoch, userIds）
     * @throws ApiException 若调用方非管理员
     */
    public List<Map<String, Object>> groups(Actor a) {
        access.requireAdmin(a);
        var groups = db.list("SELECT id,name FROM user_group WHERE tenant_id=:t", Map.of("t", a.tenantId()));
        Object epoch = db.one("SELECT security_epoch FROM tenant WHERE id=:t", Map.of("t", a.tenantId())).get("security_epoch");
        for (var g : groups) {
            g.put("securityEpoch", epoch);
            g.put("userIds", db.list("SELECT user_id FROM group_member WHERE tenant_id=:t AND group_id=:g", Db.params("t", a.tenantId(), "g", g.get("id"))).stream().map(r -> r.get("user_id")).toList());
        }
        return groups;
    }

    /**
     * 获取当前登录用户的个人信息、权限身份及所属用户组。
     *
     * @param a 当前操作主体
     * @return 包含 userId, tenantId, loginName, admin, securityEpoch 与 groups 的详情 Map
     */
    public Map<String, Object> me(Actor a) {
        var r = db.one("SELECT login_name AS loginName FROM app_user WHERE tenant_id=:t AND id=:u", Db.params("t", a.tenantId(), "u", a.userId()));
        r.put("securityEpoch", db.one("SELECT security_epoch FROM tenant WHERE id=:t", Map.of("t", a.tenantId())).get("security_epoch"));
        r.putAll(Db.params("userId", a.userId(), "tenantId", a.tenantId(), "admin", a.admin(), "groups", db.list("SELECT g.id,g.name FROM user_group g JOIN group_member m ON m.tenant_id=g.tenant_id AND m.group_id=g.id WHERE m.tenant_id=:t AND m.user_id=:u", Db.params("t", a.tenantId(), "u", a.userId()))));
        return r;
    }

    /**
     * 获取当前用户有权读取的活跃知识库列表。
     *
     * @param a 当前操作主体
     * @return 授权知识库列表（含 id, name, rowVersion）
     */
    public List<Map<String, Object>> knowledgeBases(Actor a) {
        return db.list("SELECT kb.id,kb.name,kb.row_version AS rowVersion FROM knowledge_base kb WHERE kb.tenant_id=:t AND kb.status='LIVE' AND EXISTS (SELECT 1 FROM kb_grant kg WHERE kg.tenant_id=kb.tenant_id AND kg.kb_id=kb.id AND " + AccessPolicy.principal("kg") + ")", Db.params("t", a.tenantId(), "u", a.userId()));
    }

    /**
     * 创建新知识库并初始化其访问权限列表。
     * 需租户写锁保护，自动递增 security_epoch 并记录审计日志。
     *
     * @param a    当前操作主体（需管理员）
     * @param body 包含 name 及 readers 授权列表的请求体
     * @return 创建完成的知识库简要信息
     */
    public Map<String, Object> createKb(Actor a, Map<String, Object> body) {
        access.requireAdmin(a);
        String name = required(body, "name", 255);
        return access.write(a, () -> tx.execute(s -> {
            String id = Json.id();
            db.insert("knowledge_base", Db.params("tenant_id", a.tenantId(), "id", id, "name", name));
            replaceGrants(a, "kb_grant", "kb_id", id, readers(body));
            epoch(a);
            audit(a, "KB_CREATE", "KB", id);
            return Map.of("id", id, "name", name, "rowVersion", 0);
        }));
    }

    /**
     * 查询知识库或文档当前的 ACL 详情及授权读者列表。
     *
     * @param a        当前操作主体（需管理员）
     * @param id       目标知识库或文档 ID
     * @param document true 表示文档，false 表示知识库
     * @return ACL 详细信息（含 rowVersion, acl_mode, readers）
     */
    public Map<String, Object> acl(Actor a, String id, boolean document) {
        access.requireAdmin(a);
        String table = document ? "document" : "knowledge_base", grants = document ? "document_grant" : "kb_grant", fk = document ? "document_id" : "kb_id";
        var r = db.one("SELECT id,row_version AS rowVersion" + (document ? ",acl_mode AS mode" : "") + " FROM " + table + " WHERE tenant_id=:t AND id=:id", Db.params("t", a.tenantId(), "id", id));
        r.put("readers", db.list("SELECT principal_type AS type,user_id AS userId,group_id AS groupId FROM " + grants + " WHERE tenant_id=:t AND " + fk + "=:id", Db.params("t", a.tenantId(), "id", id)));
        return r;
    }

    /**
     * 更新知识库或文档的 ACL 配置。
     * <p>
     * <b>重要并发控制逻辑：</b>
     * 1. 采用乐观锁机制：基于 expectedVersion 比对并递增 row_version，冲突时抛出 409；
     * 2. 在租户独占写锁与数据库事务内原子执行授权记录覆盖（replaceGrants）；
     * 3. 递增租户安全纪元（security_epoch）并记录审计事件。
     *
     * @param a        当前操作主体（需管理员）
     * @param id       知识库或文档 ID
     * @param document true 表示文档，false 表示知识库
     * @param body     包含 expectedVersion、mode（仅文档）及 readers 的请求体
     * @return 更新后的最新 ACL 数据
     * @throws ApiException 若版本冲突（409 VERSION_CONFLICT）或参数无效（400 INVALID_INPUT）
     */
    public Map<String, Object> setAcl(Actor a, String id, boolean document, Map<String, Object> body) {
        access.requireAdmin(a);
        long expected = number(body, "expectedVersion");
        String table = document ? "document" : "knowledge_base", mode = Json.str(body, "mode");
        if (document && !Set.of("INHERIT", "RESTRICT").contains(mode)) throw invalid("mode 无效");
        return access.write(a, () -> tx.execute(s -> {
            var p = Db.params("t", a.tenantId(), "id", id, "v", expected, "mode", mode);
            int n = db.exec("UPDATE " + table + " SET row_version=row_version+1" + (document ? ",acl_mode=:mode" : "") + " WHERE tenant_id=:t AND id=:id AND row_version=:v", p);
            if (n != 1) throw conflict();
            replaceGrants(a, document ? "document_grant" : "kb_grant", document ? "document_id" : "kb_id", id, readers(body));
            epoch(a);
            audit(a, "ACL_UPDATE", document ? "DOCUMENT" : "KB", id);
            return acl(a, id, document);
        }));
    }

    /**
     * 更新用户组的成员列表。
     * 基于 expectedSecurityEpoch 乐观并发控制，原子性重置该组的所有成员。
     *
     * @param a     当前操作主体（需管理员）
     * @param group 用户组 ID
     * @param body  包含 expectedSecurityEpoch 及 userIds 数组的请求体
     * @return 更新结果状态 Map
     * @throws ApiException 若安全纪元不一致（409 VERSION_CONFLICT）或用户 ID 不存在
     */
    public Map<String, Object> members(Actor a, String group, Map<String, Object> body) {
        access.requireAdmin(a);
        return access.write(a, () -> tx.execute(s -> {
            db.one("SELECT id FROM user_group WHERE tenant_id=:t AND id=:id", Db.params("t", a.tenantId(), "id", group));
            if (db.exec("UPDATE tenant SET security_epoch=security_epoch+1 WHERE id=:t AND security_epoch=:v", Db.params("t", a.tenantId(), "v", number(body, "expectedSecurityEpoch"))) != 1)
                throw conflict();
            Object raw = body.get("userIds");
            if (!(raw instanceof List<?> ids) || ids.size() > 1000) throw invalid("userIds 无效");
            db.exec("DELETE FROM group_member WHERE tenant_id=:t AND group_id=:g", Db.params("t", a.tenantId(), "g", group));
            for (Object id : new LinkedHashSet<>(ids)) {
                db.one("SELECT id FROM app_user WHERE tenant_id=:t AND id=:id", Db.params("t", a.tenantId(), "id", id));
                db.insert("group_member", Db.params("tenant_id", a.tenantId(), "id", Json.id(), "group_id", group, "user_id", id));
            }
            audit(a, "GROUP_MEMBERS_UPDATE", "GROUP", group);
            return Map.of("id", group, "updated", true);
        }));
    }

    /**
     * 全量替换指定实体（知识库或文档）的授权记录。
     * 校验授权主体类型（USER / GROUP / ALL，注意文档级禁止直接使用 ALL），并验证用户和组在当前租户内真实有效。
     *
     * @param a       当前操作主体
     * @param table   目标授权表名（kb_grant 或 document_grant）
     * @param fk      外键字段名（kb_id 或 document_id）
     * @param id      外键实体 ID
     * @param readers 待设置的授权主体列表
     * @throws ApiException 若主体类型非法、文档使用 ALL 或关联主体 ID 不存在
     */
    public void replaceGrants(Actor a, String table, String fk, String id, List<Map<String, Object>> readers) {
        db.exec("DELETE FROM " + table + " WHERE tenant_id=:t AND " + fk + "=:id", Db.params("t", a.tenantId(), "id", id));
        Set<String> seen = new HashSet<>();
        for (var r : readers) {
            String type = Json.str(r, "type").toUpperCase(Locale.ROOT), user = Json.str(r, "userId"), group = Json.str(r, "groupId");
            if (!Set.of("USER", "GROUP", "ALL").contains(type) || type.equals("ALL") && table.equals("document_grant"))
                throw invalid("授权主体无效");
            if (type.equals("USER"))
                db.one("SELECT id FROM app_user WHERE tenant_id=:t AND id=:id", Db.params("t", a.tenantId(), "id", user));
            if (type.equals("GROUP"))
                db.one("SELECT id FROM user_group WHERE tenant_id=:t AND id=:id", Db.params("t", a.tenantId(), "id", group));
            if (!seen.add(type + user + group)) continue;
            db.insert(table, Db.params("tenant_id", a.tenantId(), "id", Json.id(), fk, id, "principal_type", type, "user_id", type.equals("USER") ? user : null, "group_id", type.equals("GROUP") ? group : null));
        }
    }

    /**
     * 从请求体中提取并校验 readers 授权主体列表。
     *
     * @param body 请求体数据 Map
     * @return 经过格式与大小（上限 1000）校验的授权主体 Map 列表
     * @throws ApiException 若 readers 不是有效列表或数量超过 1000
     */
    @SuppressWarnings("unchecked")
    public static List<Map<String, Object>> readers(Map<String, Object> body) {
        Object v = body.get("readers");
        if (!(v instanceof List<?> list) || list.size() > 1000 || list.stream().anyMatch(x -> !(x instanceof Map)))
            throw invalid("readers 必须是授权列表");
        return (List<Map<String, Object>>) v;
    }

    /**
     * 校验并提取必填字符串字段，防止超长输入。
     *
     * @param b   请求体 Map
     * @param key 目标键名
     * @param max 最大允许字符长度
     * @return 去除首尾空白后的非空字符串
     * @throws ApiException 若缺失或长度超标（400 INVALID_INPUT）
     */
    public static String required(Map<String, Object> b, String key, int max) {
        String s = Json.str(b, key).strip();
        if (s.isEmpty() || s.length() > max) throw invalid(key + " 无效");
        return s;
    }

    /**
     * 校验并提取长整型数字段。
     *
     * @param b   请求体 Map
     * @param key 目标键名
     * @return 解析后的 long 数值
     * @throws ApiException 若无法解析为数值（400 INVALID_INPUT）
     */
    public static long number(Map<String, Object> b, String key) {
        try {
            return Long.parseLong(required(b, key, 20));
        } catch (NumberFormatException e) {
            throw invalid(key + " 无效");
        }
    }

    /**
     * 构造 400 INVALID_INPUT 参数错误异常。
     *
     * @param m 错误提示文本
     * @return ApiException 实例
     */
    public static ApiException invalid(String m) {
        return new ApiException(400, "INVALID_INPUT", m);
    }

    /**
     * 构造 409 VERSION_CONFLICT 乐观锁版本并发冲突异常。
     *
     * @return ApiException 实例
     */
    public static ApiException conflict() {
        return new ApiException(409, "VERSION_CONFLICT", "数据已变化，请刷新后重试");
    }
}

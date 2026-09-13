package dev.rag.identity;

import dev.rag.common.*;
import jakarta.servlet.http.*;
import org.springframework.stereotype.Service;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.locks.ReentrantReadWriteLock;
import java.util.function.Supplier;

/**
 * 核心访问控制策略组件（RBAC / ABAC / 多租户隔离）。
 * <p>
 * <b>安全设计准则：</b>
 * 1. 数据库 SQL 作为唯一权限权威（SQL is the authority），杜绝在内存缓存中绕过权限判断。
 * 2. 管理员角色（Admin）仅代表运维管理能力，严禁作为内容越权（Content Bypass）的后门。
 * 3. 租户隔离通过读写锁（ReentrantReadWriteLock）与行级租户标识双重保障，避免并发读写竞争下的越权读取。
 */
@Service
public class AccessPolicy {
    private final Db db;
    /** 每个租户独立的读写锁映射，用于在 ACL 变更与内容检索期间提供内存级并发屏障 */
    private final Map<String, ReentrantReadWriteLock> locks = new ConcurrentHashMap<>();

    /**
     * 构造 AccessPolicy 实例。
     *
     * @param db 数据库操作组件
     */
    public AccessPolicy(Db db) {
        this.db = db;
    }

    /**
     * 计算字符串的 UTF-8 字节 SHA-256 哈希值。
     *
     * @param value 原始字符串
     * @return 32 字节的 SHA-256 摘要
     */
    public static byte[] hash(String value) {
        return hash(value.getBytes(StandardCharsets.UTF_8));
    }

    /**
     * 计算原始字节数组的 SHA-256 哈希值。
     *
     * @param value 字节数组
     * @return 32 字节的 SHA-256 摘要
     * @throws IllegalStateException 若底层算法环境不支持 SHA-256
     */
    public static byte[] hash(byte[] value) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(value);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    /**
     * 从 HTTP 请求中提取指定名称的 Cookie 值。
     *
     * @param r    当前 HttpServletRequest
     * @param name Cookie 名称
     * @return 匹配的 Cookie 值，未找到则返回空字符串 ""
     */
    public static String cookie(HttpServletRequest r, String name) {
        if (r.getCookies() != null) for (Cookie c : r.getCookies()) if (c.getName().equals(name)) return c.getValue();
        return "";
    }

    /**
     * 解析并验证当前请求的操作主体（Actor）。
     * <p>
     * <b>重要安全方法：</b>
     * 1. 从 HTTP Cookie 中读取 RAG_SESSION 令牌；
     * 2. 计算 Session Token 的 SHA-256 摘要，联查 auth_session 与 app_user 表；
     * 3. 强制校验 Session 处于未撤销状态（revoked_at IS NULL）、有效期内（expires_at > UTC_TIMESTAMP）且用户状态为 ACTIVE；
     * 4. 构造包含租户 ID、用户 ID 和管理员角色的不可变 Actor 实体。
     *
     * @param request 当前 HTTP 请求
     * @return 认证通过的操作主体 Actor
     * @throws ApiException 若未登录（401 AUTH_REQUIRED）或会话已过期/已撤销
     */
    public Actor actor(HttpServletRequest request) {
        String token = cookie(request, "RAG_SESSION");
        if (token.isBlank()) throw new ApiException(401, "AUTH_REQUIRED", "请先登录");
        var row = db.optional("SELECT u.tenant_id,u.id,u.is_admin FROM auth_session s JOIN app_user u ON u.tenant_id=s.tenant_id AND u.id=s.user_id WHERE s.token_hash=:h AND s.revoked_at IS NULL AND s.expires_at>UTC_TIMESTAMP(6) AND u.status='ACTIVE'", Map.of("h", hash(token)))
                .orElseThrow(() -> new ApiException(401, "AUTH_REQUIRED", "登录状态已失效"));
        return new Actor(row.get("tenant_id").toString(), row.get("id").toString(), Boolean.TRUE.equals(row.get("is_admin")) || "1".equals(row.get("is_admin").toString()));
    }

    /**
     * 校验当前操作主体是否具备管理员权限。
     *
     * @param a 当前操作主体
     * @throws ApiException 若非管理员，抛出 403 OPERATION_FORBIDDEN
     */
    public void requireAdmin(Actor a) {
        if (!a.admin()) throw new ApiException(403, "OPERATION_FORBIDDEN", "需要维护权限");
    }

    /**
     * 生成主体授权匹配的 SQL 条件片段。
     * 覆盖三种主体授权类型：所有人(ALL)、指定用户(user_id=:u) 或 用户所在用户组(group_member)。
     *
     * @param alias 授权表别名（如 kg 或 dg）
     * @return 拼接后的 SQL 片段
     */
    public static String principal(String alias) {
        return "(" + alias + ".principal_type='ALL' OR " + alias + ".user_id=:u OR EXISTS (SELECT 1 FROM group_member gm WHERE gm.tenant_id=:t AND gm.user_id=:u AND gm.group_id=" + alias + ".group_id))";
    }

    /**
     * 生成文档可读性判定核心 SQL 条件片段。
     * <p>
     * <b>多层访问控制判定逻辑：</b>
     * 1. 租户隔离：d.tenant_id=:t；
     * 2. 文档与用户存活状态：d.status='LIVE' 且 用户状态为 ACTIVE；
     * 3. 知识库级授权：所属知识库 status='LIVE' 且用户匹配知识库授权主体（principal）；
     * 4. 文档级 ACL：若 acl_mode 为 INHERIT（继承）则自动放行；若为 RESTRICT（限制）则必须显式在 document_grant 拥有授权。
     *
     * @return 完整的文档可读性 SQL 判定条件
     */
    public static String readable() {
        return "d.tenant_id=:t AND d.status='LIVE' AND EXISTS (SELECT 1 FROM app_user au WHERE au.tenant_id=:t AND au.id=:u AND au.status='ACTIVE') AND EXISTS (SELECT 1 FROM knowledge_base kb JOIN kb_grant kg ON kg.tenant_id=kb.tenant_id AND kg.kb_id=kb.id WHERE kb.tenant_id=d.tenant_id AND kb.id=d.kb_id AND kb.status='LIVE' AND " + principal("kg") + ") AND (d.acl_mode='INHERIT' OR EXISTS (SELECT 1 FROM document_grant dg WHERE dg.tenant_id=d.tenant_id AND dg.document_id=d.id AND " + principal("dg") + "))";
    }

    /**
     * 检查当前用户是否有权读取指定知识库。
     *
     * @param a  操作主体
     * @param id 知识库 ID
     * @return true 表示有权读取，false 表示无权访问
     */
    public boolean canReadKb(Actor a, String id) {
        return !db.list("SELECT kb.id FROM knowledge_base kb WHERE kb.tenant_id=:t AND kb.id=:id AND kb.status='LIVE' AND EXISTS (SELECT 1 FROM kb_grant kg WHERE kg.tenant_id=kb.tenant_id AND kg.kb_id=kb.id AND " + principal("kg") + ")", Db.params("t", a.tenantId(), "u", a.userId(), "id", id)).isEmpty();
    }

    /**
     * 检查当前用户是否有权读取指定文档。
     *
     * @param a  操作主体
     * @param id 文档 ID
     * @return true 表示有权读取，false 表示无权访问
     */
    public boolean canReadDocument(Actor a, String id) {
        return !db.list("SELECT d.id FROM document d WHERE " + readable() + " AND d.id=:id", Db.params("t", a.tenantId(), "u", a.userId(), "id", id)).isEmpty();
    }

    /**
     * 断言当前用户必须有权读取指定文档，若不可访问则抛出 404。
     *
     * @param a  操作主体
     * @param id 文档 ID
     * @throws ApiException 若文档不存在或无权限读取（404 RESOURCE_NOT_FOUND）
     */
    public void requireDocument(Actor a, String id) {
        if (!canReadDocument(a, id)) throw ApiException.missing();
    }

    /**
     * 获取指定知识库集合中，当前用户有权访问的所有文档的最新就绪版本（active_build_id）列表。
     * <p>
     * <b>安全与防雪崩说明：</b>
     * 检索范围严格通过 readable() 进行 SQL 级过滤，最大限制 10000 条构建版本，超出则熔断防范大范围全表扫描。
     *
     * @param a     操作主体
     * @param kbIds 目标知识库 ID 列表，传空或 null 时表示租户下全部授权库
     * @return 可读文档的 active_build_id 列表
     * @throws ApiException 若命中文档数超过 10000 条限制（422 SCOPE_TOO_LARGE）
     */
    public List<String> readableBuilds(Actor a, List<String> kbIds) {
        var p = Db.params("t", a.tenantId(), "u", a.userId());
        String filter = "";
        if (kbIds != null && !kbIds.isEmpty()) {
            filter = " AND d.kb_id IN (:kb)";
            p.put("kb", kbIds);
        }
        var rows = db.list("SELECT d.active_build_id FROM document d WHERE " + readable() + " AND d.active_build_id IS NOT NULL" + filter + " LIMIT 10001", p);
        if (rows.size() > 10000) throw new ApiException(422, "SCOPE_TOO_LARGE", "请缩小知识库范围");
        return rows.stream().map(r -> r.get("active_build_id").toString()).toList();
    }

    /**
     * 读操作并发保护闭包。
     * <p>
     * 获取当前租户的公平读锁，校验目标文档集合的读取权限，并在持有读锁的状态下执行目标任务，
     * 防止在文档 ACL 变更过程中产生权限未生效的脏读。
     *
     * @param a    操作主体
     * @param ids  涉及访问的文档 ID 集合
     * @param work 待执行的业务逻辑 Supplier
     * @param <T>  返回值泛型
     * @return 业务执行结果
     */
    public <T> T guard(Actor a, Collection<String> ids, Supplier<T> work) {
        var lock = locks.computeIfAbsent(a.tenantId(), x -> new ReentrantReadWriteLock(true)).readLock();
        lock.lock();
        try {
            for (String id : new TreeSet<>(ids)) requireDocument(a, id);
            return work.get();
        } finally {
            lock.unlock();
        }
    }

    /**
     * 写操作并发保护闭包。
     * <p>
     * 获取当前租户的公平写锁，独占式执行 ACL、文档或用户组变更逻辑，
     * 阻止并发的读请求与写入冲突。
     *
     * @param a    操作主体
     * @param work 待执行的写操作业务逻辑 Supplier
     * @param <T>  返回值泛型
     * @return 业务执行结果
     */
    public <T> T write(Actor a, Supplier<T> work) {
        var lock = locks.computeIfAbsent(a.tenantId(), x -> new ReentrantReadWriteLock(true)).writeLock();
        lock.lock();
        try {
            return work.get();
        } finally {
            lock.unlock();
        }
    }
}

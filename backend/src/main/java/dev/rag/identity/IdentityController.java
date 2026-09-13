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

/**
 * 认证与权限管理 REST 接口控制器。
 * 提供会话登录、注销、CSRF 令牌获取、用户信息以及知识库/文档 ACL 配置等接口。
 */
@RestController
@RequestMapping("/api/v1")
public class IdentityController {
    private final Db db;
    private final AccessPolicy access;
    private final IdentityService service;
    private final CsrfProtection csrf;
    private final boolean secure;
    private final BCryptPasswordEncoder passwords = new BCryptPasswordEncoder(12);
    /** 内存级登录频控滑动窗口映射：IP:租户:账号 -> 最近 1 分钟内的登录时间戳列表 */
    private final Map<String, List<Long>> attempts = new ConcurrentHashMap<>();
    /** 虚设密码哈希，用于当用户不存在时执行空跑匹配，防止通过响应耗时差异猜测用户名存在性（抗计时攻击） */
    private final String dummy = new BCryptPasswordEncoder(12).encode("no-such-user");

    /**
     * 构造 IdentityController 实例。
     *
     * @param db      数据库访问组件
     * @param access  访问控制策略组件
     * @param service 身份与组织服务
     * @param csrf    CSRF 防护组件
     * @param secure  Cookie 是否启用 secure 标志
     */
    public IdentityController(Db db, AccessPolicy access, IdentityService service, CsrfProtection csrf, @Value("${rag.cookie-secure:false}") boolean secure) {
        this.db = db;
        this.access = access;
        this.service = service;
        this.csrf = csrf;
        this.secure = secure;
    }

    /**
     * 使用 SecureRandom 生成 32 字节高熵随机 Session 令牌，以 Base64 URL 安全格式返回。
     *
     * @return 43 位的 URL 安全高熵字符串
     */
    public static String token() {
        byte[] b = new byte[32];
        new SecureRandom().nextBytes(b);
        return Base64.getUrlEncoder().withoutPadding().encodeToString(b);
    }

    /**
     * 写入 HTTP Cookie 工具方法。
     * 强制开启 SameSite=Lax 保护与根路径 (/) 作用域。
     *
     * @param r        当前 HTTP 响应
     * @param name     Cookie 名称
     * @param value    Cookie 值
     * @param httpOnly 是否限制仅 HTTP 协议可访问（禁止 JS 读取）
     * @param secure   是否仅允许 HTTPS 连接传输
     * @param seconds  Cookie 有效秒数（0 表示立即删除）
     */
    static void cookie(HttpServletResponse r, String name, String value, boolean httpOnly, boolean secure, long seconds) {
        r.addHeader("Set-Cookie", ResponseCookie.from(name, value).httpOnly(httpOnly).secure(secure).sameSite("Lax").path("/").maxAge(seconds).build().toString());
    }

    /**
     * 获取或刷新当前客户端的 CSRF 防护令牌。
     *
     * @param r        当前 HTTP 请求
     * @param response 当前 HTTP 响应（将同步写入 RAG_CSRF Cookie）
     * @return 包含 csrfToken 的数据 Map
     */
    @GetMapping("/auth/csrf")
    public Map<String, Object> csrf(HttpServletRequest r, HttpServletResponse response) {
        return Map.of("csrfToken", csrf.issue(r, response));
    }

    /**
     * 用户账号密码认证登录。
     * <p>
     * <b>重要安全防护机制：</b>
     * 1. 暴力破解防御：基于 IP + 租户编码 + 账号 进行 60 秒滑动窗口频控，阈值上限为 5 次，超出抛出 429 RATE_LIMITED；
     * 2. 抗计时侧信道攻击：无论账号是否存在，均执行 BCrypt.matches 运算（若账号不存在则比对预计算的 dummy 散列），消除响应耗时偏差；
     * 3. 会话持久化：生成 32 字节高强度会话 Token，计算 SHA-256 存储至 auth_session，有效期 8 小时（28800 秒）；
     * 4. 安全 Cookie 回设：以 HttpOnly 模式下发 RAG_SESSION Cookie，并重置旧的 RAG_CSRF Cookie。
     *
     * @param body     包含 tenantCode、loginName、password 的请求体
     * @param r        当前 HTTP 请求
     * @param response 当前 HTTP 响应
     * @return 认证结果 Map {"authenticated": true}
     * @throws ApiException 若频率超限（429 RATE_LIMITED）或账号密码错误（401 INVALID_CREDENTIALS）
     */
    @PostMapping("/auth/login")
    public Map<String, Object> login(@RequestBody Map<String, Object> body, HttpServletRequest r, HttpServletResponse response) {
        String tenant = IdentityService.required(body, "tenantCode", 64), login = IdentityService.required(body, "loginName", 128), password = IdentityService.required(body, "password", 128);
        String key = r.getRemoteAddr() + ":" + tenant + ":" + login;
        long now = System.currentTimeMillis();
        if (attempts.size() > 10000)
            attempts.entrySet().removeIf(e -> e.getValue().stream().allMatch(t -> now - t > 60000));
        var bucket = attempts.computeIfAbsent(key, k -> new ArrayList<>());
        synchronized (bucket) {
            bucket.removeIf(t -> now - t > 60000);
            if (bucket.size() >= 5) throw new ApiException(429, "RATE_LIMITED", "登录尝试过于频繁");
            bucket.add(now);
        }
        var row = db.optional("SELECT u.* FROM app_user u JOIN tenant t ON t.id=u.tenant_id WHERE t.code=:t AND u.login_name=:u AND u.status='ACTIVE'", Db.params("t", tenant, "u", login));
        boolean valid = passwords.matches(password, row.map(x -> x.get("password_hash").toString()).orElse(dummy));
        if (row.isEmpty() || !valid) throw new ApiException(401, "INVALID_CREDENTIALS", "账号或密码错误");
        var user = row.get();
        String token = token();
        db.insert("auth_session", Db.params("tenant_id", user.get("tenant_id"), "id", Json.id(), "user_id", user.get("id"), "token_hash", AccessPolicy.hash(token), "expires_at", java.sql.Timestamp.from(Instant.now().plusSeconds(28800))));
        cookie(response, "RAG_SESSION", token, true, secure, 28800);
        cookie(response, "RAG_CSRF", "", false, secure, 0);
        return Map.of("authenticated", true);
    }

    /**
     * 退出登录并撤销当前会话。
     * 将数据库中当前 Token 对应记录的 revoked_at 置为当前时间戳，并清除客户端 Cookie。
     *
     * @param r        当前 HTTP 请求
     * @param response 当前 HTTP 响应
     * @return 退出结果 Map {"authenticated": false}
     */
    @PostMapping("/auth/logout")
    public Map<String, Object> logout(HttpServletRequest r, HttpServletResponse response) {
        Actor a = access.actor(r);
        db.exec("UPDATE auth_session SET revoked_at=UTC_TIMESTAMP(6) WHERE tenant_id=:t AND token_hash=:h", Db.params("t", a.tenantId(), "h", AccessPolicy.hash(AccessPolicy.cookie(r, "RAG_SESSION"))));
        cookie(response, "RAG_SESSION", "", true, secure, 0);
        return Map.of("authenticated", false);
    }

    /**
     * 获取当前登录用户的个人基本信息及权限配置。
     *
     * @param r 当前 HTTP 请求
     * @return 当前用户资料 Map
     */
    @GetMapping("/me")
    public Map<String, Object> me(HttpServletRequest r) {
        return service.me(access.actor(r));
    }

    /**
     * 查询当前登录用户有权访问的知识库列表。
     *
     * @param r 当前 HTTP 请求
     * @return 授权的知识库列表
     */
    @GetMapping("/knowledge-bases")
    public Object kbs(HttpServletRequest r) {
        return service.knowledgeBases(access.actor(r));
    }

    /**
     * 创建知识库（需管理员权限）。
     *
     * @param r 当前 HTTP 请求
     * @param b 包含 name 及 readers 授权列表的请求体
     * @return 创建完成的知识库简要数据
     */
    @PostMapping("/admin/knowledge-bases")
    public Object create(HttpServletRequest r, @RequestBody Map<String, Object> b) {
        return service.createKb(access.actor(r), b);
    }

    /**
     * 查询指定知识库的 ACL 权限配置详情（需管理员权限）。
     *
     * @param r  当前 HTTP 请求
     * @param id 知识库 ID
     * @return ACL 详细配置
     */
    @GetMapping("/admin/knowledge-bases/{id}/acl")
    public Object kbAcl(HttpServletRequest r, @PathVariable String id) {
        return service.acl(access.actor(r), id, false);
    }

    /**
     * 更新指定知识库的 ACL 权限配置（需管理员权限）。
     *
     * @param r  当前 HTTP 请求
     * @param id 知识库 ID
     * @param b  更新参数体（包含 expectedVersion 及 readers）
     * @return 更新后的最新 ACL 数据
     */
    @PutMapping("/admin/knowledge-bases/{id}/acl")
    public Object kbAcl(HttpServletRequest r, @PathVariable String id, @RequestBody Map<String, Object> b) {
        return service.setAcl(access.actor(r), id, false, b);
    }

    /**
     * 查询指定文档的 ACL 权限配置详情（需管理员权限）。
     *
     * @param r  当前 HTTP 请求
     * @param id 文档 ID
     * @return ACL 详细配置
     */
    @GetMapping("/admin/documents/{id}/acl")
    public Object docAcl(HttpServletRequest r, @PathVariable String id) {
        return service.acl(access.actor(r), id, true);
    }

    /**
     * 更新指定文档的 ACL 权限配置（需管理员权限）。
     *
     * @param r  当前 HTTP 请求
     * @param id 文档 ID
     * @param b  更新参数体（包含 expectedVersion、mode 及 readers）
     * @return 更新后的最新 ACL 数据
     */
    @PutMapping("/admin/documents/{id}/acl")
    public Object docAcl(HttpServletRequest r, @PathVariable String id, @RequestBody Map<String, Object> b) {
        return service.setAcl(access.actor(r), id, true, b);
    }

    /**
     * 查询租户下所有用户列表（需管理员权限）。
     *
     * @param r 当前 HTTP 请求
     * @return 用户列表
     */
    @GetMapping("/admin/users")
    public Object users(HttpServletRequest r) {
        return service.users(access.actor(r));
    }

    /**
     * 查询租户下所有用户组及成列表（需管理员权限）。
     *
     * @param r 当前 HTTP 请求
     * @return 用户组列表
     */
    @GetMapping("/admin/groups")
    public Object groups(HttpServletRequest r) {
        return service.groups(access.actor(r));
    }

    /**
     * 更新指定用户组的成员名单（需管理员权限）。
     *
     * @param r  当前 HTTP 请求
     * @param id 用户组 ID
     * @param b  更新参数体（包含 expectedSecurityEpoch 及 userIds）
     * @return 更新结果状态
     */
    @PutMapping("/admin/groups/{id}/members")
    public Object members(HttpServletRequest r, @PathVariable String id, @RequestBody Map<String, Object> b) {
        return service.members(access.actor(r), id, b);
    }
}

package dev.rag.identity;

import dev.rag.common.*;
import jakarta.servlet.http.*;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import org.springframework.web.servlet.*;
import org.springframework.web.servlet.config.annotation.*;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.net.URI;
import java.util.*;

/**
 * 跨站请求伪造（CSRF）防护拦截器与 MVC 配置。
 * <p>
 * <b>安全机制：</b>
 * 1. 采用 Cookie-to-Header 绑定令牌方案，令牌结构为 {@code timestamp.randomToken.signature}；
 * 2. 签名采用 HMAC-SHA256，结合用户的 Session Token 或未登录 Nonce，防止令牌被其他会话复用；
 * 3. 对非幂等操作（POST、PUT、DELETE 等）校验 Origin 来源头与 Header 中的 X-CSRF-Token。
 */
@Component
public class CsrfProtection implements HandlerInterceptor, WebMvcConfigurer {
    private final byte[] key;
    private final boolean secure;

    /**
     * 构造 CSRF 防护拦截器。
     *
     * @param secret 配置中指定的 CSRF 密钥，若为空则自动生成随机安全密钥
     * @param secure Cookie 是否启用 Secure 属性（HTTPS 专用）
     */
    public CsrfProtection(@Value("${rag.csrf-secret:}") String secret, @Value("${rag.cookie-secure:false}") boolean secure) {
        this.key = (secret.isBlank() ? IdentityController.token() : secret).getBytes(StandardCharsets.UTF_8);
        this.secure = secure;
    }

    /**
     * 注册拦截器到 Spring MVC 管道。
     * 拦截所有 /api/v1/** 路径，豁免 /api/v1/internal/** 内部调用。
     *
     * @param registry 拦截器注册表
     */
    @Override
    public void addInterceptors(InterceptorRegistry registry) {
        registry.addInterceptor(this).addPathPatterns("/api/v1/**").excludePathPatterns("/api/v1/internal/**");
    }

    /**
     * 使用 HMAC-SHA256 计算输入数据的签名。
     *
     * @param data 待签名的原始文本
     * @return Base64 URL 安全格式的签名字符串
     */
    private String sign(String data) {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(key, "HmacSHA256"));
            return Base64.getUrlEncoder().withoutPadding().encodeToString(mac.doFinal(data.getBytes(StandardCharsets.UTF_8)));
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    /**
     * 获取当前会话绑定标识（已登录绑定 RAG_SESSION，未登录绑定临时 RAG_NONCE）。
     *
     * @param r 当前 HTTP 请求
     * @return 绑定的 Token 凭据字符串
     */
    private String binding(HttpServletRequest r) {
        String s = AccessPolicy.cookie(r, "RAG_SESSION");
        return s.isBlank() ? AccessPolicy.cookie(r, "RAG_NONCE") : s;
    }

    /**
     * 为当前会话签发新的 CSRF 防护令牌，并写入 RAG_CSRF Cookie。
     *
     * @param r        当前 HTTP 请求
     * @param response 当前 HTTP 响应
     * @return 签发并签名的三段式 CSRF 令牌字符串
     */
    public String issue(HttpServletRequest r, HttpServletResponse response) {
        String b = binding(r);
        if (b.isBlank()) {
            b = IdentityController.token();
            IdentityController.cookie(response, "RAG_NONCE", b, true, secure, 3600);
        }
        String raw = (System.currentTimeMillis() + 3600000) + "." + IdentityController.token();
        String token = raw + "." + sign(b + ":" + raw);
        IdentityController.cookie(response, "RAG_CSRF", token, false, secure, 3600);
        return token;
    }

    /**
     * 请求预处理拦截校验。
     * <p>
     * <b>校验流程：</b>
     * 1. GET / HEAD / OPTIONS 幂等请求直接放行；
     * 2. 若存在 Origin 请求头，严格比对协议、域名与端口是否与当前服务器一致；
     * 3. 校验请求头 X-CSRF-Token 与 Cookie 中的 RAG_CSRF 相同（常量时间比对）；
     * 4. 解析令牌三段结构，验证未过期且 HMAC-SHA256 签名匹配。
     *
     * @param r        当前 HTTP 请求
     * @param response 当前 HTTP 响应
     * @param handler  目标处理器
     * @return true 允许请求通过
     * @throws ApiException 若来源不匹配、令牌缺失、格式错误、已过期或签名无效（403 CSRF_INVALID）
     */
    @Override
    public boolean preHandle(HttpServletRequest r, HttpServletResponse response, Object handler) {
        if (Set.of("GET", "HEAD", "OPTIONS").contains(r.getMethod())) return true;
        String origin = r.getHeader("Origin");
        if (origin != null) {
            try {
                URI u = URI.create(origin);
                int port = u.getPort() < 0 ? (u.getScheme().equals("https") ? 443 : 80) : u.getPort();
                if (!Objects.equals(u.getScheme(), r.getScheme()) || !Objects.equals(u.getHost(), r.getServerName()) || port != r.getServerPort())
                    throw denied();
            } catch (IllegalArgumentException e) {
                throw denied();
            }
        }
        String header = r.getHeader("X-CSRF-Token"), cookie = AccessPolicy.cookie(r, "RAG_CSRF");
        if (header == null || !MessageDigest.isEqual(header.getBytes(StandardCharsets.UTF_8), cookie.getBytes(StandardCharsets.UTF_8)))
            throw denied();
        String[] p = header.split("\\.");
        if (p.length != 3) throw denied();
        try {
            if (Long.parseLong(p[0]) < System.currentTimeMillis() || !MessageDigest.isEqual(p[2].getBytes(StandardCharsets.UTF_8), sign(binding(r) + ":" + p[0] + "." + p[1]).getBytes(StandardCharsets.UTF_8)))
                throw denied();
        } catch (NumberFormatException e) {
            throw denied();
        }
        return true;
    }

    /**
     * 构造 CSRF 校验失败的标准业务异常。
     *
     * @return 403 CSRF_INVALID 异常
     */
    private ApiException denied() {
        return new ApiException(403, "CSRF_INVALID", "请刷新安全凭据后重试");
    }
}

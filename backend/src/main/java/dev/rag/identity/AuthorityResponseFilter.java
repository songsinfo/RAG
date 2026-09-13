package dev.rag.identity;

import dev.rag.common.*;
import jakarta.servlet.*;
import jakarta.servlet.http.*;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.util.*;
import java.util.function.Supplier;

/**
 * 租户权限门禁与响应序列化过滤器。
 * <p>
 * <b>并发安全设计：</b>
 * 将租户的读写锁一直保持到响应正文序列化刷盘完成（response.flushBuffer()），
 * 彻底消除“Controller 执行完毕但数据尚未完全写回客户端时权限已被撤销”的读取竞争漏洞。
 */
@Component
public class AuthorityResponseFilter extends OncePerRequestFilter {
    private final AccessPolicy access;

    /**
     * 构造 AuthorityResponseFilter 实例。
     *
     * @param access 访问控制策略组件
     */
    public AuthorityResponseFilter(AccessPolicy access) {
        this.access = access;
    }

    /**
     * 判定当前请求是否豁免过滤。
     * 豁免非 /api/v1/ 路径、认证接口 (/auth/)、内部接口 (/internal/) 及健康检查 (/health/)。
     *
     * @param r 当前 HTTP 请求
     * @return true 表示豁免过滤，false 表示需进行租户门禁控制
     */
    protected boolean shouldNotFilter(HttpServletRequest r) {
        String p = r.getRequestURI();
        return !p.startsWith("/api/v1/") || p.startsWith("/api/v1/auth/") || p.startsWith("/api/v1/internal/") || p.startsWith("/api/v1/health/");
    }

    /**
     * 核心过滤执行逻辑。
     * 1. 验证 Actor 身份；
     * 2. 对读请求 (GET/HEAD/OPTIONS) 施加租户读锁，对写请求施加租户写锁；
     * 3. 在锁保护块内执行 Filter 链调用并 flushBuffer()；
     * 4. 捕获并统一输出 ApiException JSON 错误响应。
     *
     * @param request  当前 HTTP 请求
     * @param response 当前 HTTP 响应
     * @param chain    过滤器执行链
     * @throws ServletException Servlet 处理异常
     * @throws IOException      IO 异常
     */
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response, FilterChain chain) throws ServletException, IOException {
        try {
            Actor a = access.actor(request);
            Supplier<Void> process = () -> {
                try {
                    response.setHeader("Cache-Control", "no-store");
                    chain.doFilter(request, response);
                    response.flushBuffer();
                    return null;
                } catch (IOException e) {
                    throw new UncheckedIOException(e);
                } catch (ServletException e) {
                    throw new ServletFailure(e);
                }
            };
            if (Set.of("GET", "HEAD", "OPTIONS").contains(request.getMethod())) access.guard(a, List.of(), process);
            else access.write(a, process);
        } catch (ApiException e) {
            if (response.isCommitted()) throw e;
            response.setStatus(e.status);
            response.setContentType("application/json;charset=UTF-8");
            response.getWriter().write(Json.write(Map.of("code", e.code, "message", e.getMessage(), "requestId", Json.id(), "retryable", e.status >= 500, "details", Map.of())));
        } catch (UncheckedIOException e) {
            throw e.getCause();
        } catch (ServletFailure e) {
            throw (ServletException) e.getCause();
        }
    }

    /**
     * 用于在内部 Supplier 闭包中传递 ServletException 的运行时异常包装类。
     */
    private static class ServletFailure extends RuntimeException {
        ServletFailure(ServletException e) {
            super(e);
        }
    }
}

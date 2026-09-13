package dev.rag.model;

import dev.rag.common.*;
import dev.rag.identity.AccessPolicy;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.web.bind.annotation.*;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.math.BigDecimal;
import java.util.*;

import static dev.rag.common.Db.params;

/**
 * 预算与额度管控 HTTP 控制器。
 * <p>
 * 提供面向租户管理员的预算消耗查询端点（/admin/budget），以及面向内部微服务间/跨环境协作的内部远程预算授权协议端点：
 * <ul>
 *   <li>{@code POST /internal/budget/reservations}：远程预扣配额并返回带 HMAC 签名的价格模型快照；</li>
 *   <li>{@code POST /internal/budget/reservations/{id}/settle}：验签并执行实际用量最终结算；</li>
 *   <li>安全防护：通过固定 Bearer Token 鉴权，且显式禁止携带 Origin 标头（拦截任何浏览器跨域端点调用）。</li>
 * </ul>
 */
@RestController
@RequestMapping("/api/v1")
public class BudgetController {
    private final BudgetService budgets;
    private final AccessPolicy access;
    private final String token, tenant, caller;

    /**
     * 构造预算控制器。
     *
     * @param budgets 预算核心业务服务
     * @param access 访问控制组件
     * @param token 内部微服务认证共享 Token
     * @param tenant 内部调用默认绑定的租户 ID
     * @param caller 允许发起预算调用的环境标识（默认 evaluation）
     */
    public BudgetController(BudgetService budgets, AccessPolicy access, @Value("${rag.internal-budget-token:}") String token, @Value("${rag.internal-budget-tenant:}") String tenant, @Value("${rag.internal-budget-caller:evaluation}") String caller) {
        this.budgets = budgets;
        this.access = access;
        this.token = token;
        this.tenant = tenant;
        this.caller = caller;
    }

    /**
     * 管理员查询当前租户的预算总览及最近预扣流水。
     *
     * @param r HTTP 请求对象
     * @return 包含月度限额、已消耗金额、当前预扣中金额及近 10 条流水的统计 Map
     */
    @GetMapping("/admin/budget")
    public Object get(HttpServletRequest r) {
        var a = access.actor(r);
        access.requireAdmin(a);
        return budgets.summary(a);
    }

    /**
     * 为预算预扣流水及其计费价格快照生成 HMAC-SHA256 防篡改数字签名。
     *
     * @param id 预扣流水 ID
     * @param price 计费模型价格 Map
     * @return URL 安全的 Base64 编码签名字符串
     */
    private String sign(String id, Map<String, Object> price) {
        try {
            var mac = javax.crypto.Mac.getInstance("HmacSHA256");
            mac.init(new javax.crypto.spec.SecretKeySpec(token.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
            return Base64.getUrlEncoder().withoutPadding().encodeToString(mac.doFinal((id + Json.write(new TreeMap<>(price))).getBytes(StandardCharsets.UTF_8)));
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    /**
     * 内部微服务调用身份鉴权。
     * <p>
     * 校验 Authorization Bearer Token 是否匹配配置密钥；若携带 Origin 请求头则直接拒绝，防止浏览器客户端越权探测内部接口。
     *
     * @param r HTTP 请求对象
     * @return 内部调用主体 Actor（绑定内置租户 ID）
     * @throws ApiException 若未配置 Token、未通过校验或含有 Origin 请求头
     */
    private Actor internal(HttpServletRequest r) {
        String header = r.getHeader("Authorization");
        if (token.isBlank() || tenant.isBlank() || header == null || !MessageDigest.isEqual(("Bearer " + token).getBytes(StandardCharsets.UTF_8), header.getBytes(StandardCharsets.UTF_8)) || r.getHeader("Origin") != null)
            throw new ApiException(403, "OPERATION_FORBIDDEN", "内部服务认证失败");
        return new Actor(tenant, "", false);
    }

    /**
     * 内部远程预算预扣接口。
     * <p>
     * 校验调用方运行环境标识与 UUID 格式，根据当前有效计费单价计算预扣上限，
     * 并在数据库中插入状态为 RESERVED 的预扣流水，随响应返回 HMAC 签名的价格快照。
     *
     * @param r HTTP 请求对象
     * @param body 请求负载（包含 callerEnvironment, callId, maxInputTokens, maxOutputTokens）
     * @return 包含 reservationId, status, pricingSnapshot, pricingSignature, reservedAmount 的 Map
     * @throws ApiException 400（参数不合规）或 403（调用方环境不匹配）
     */
    @PostMapping("/internal/budget/reservations")
    public Object reserve(HttpServletRequest r, @RequestBody Map<String, Object> body) {
        var a = internal(r);
        if (!caller.equals(Json.str(body, "callerEnvironment")))
            throw new ApiException(403, "OPERATION_FORBIDDEN", "未知预算调用方");
        String id = Json.str(body, "callId");
        try {
            UUID.fromString(id);
        } catch (Exception e) {
            throw new ApiException(400, "INVALID_INPUT", "调用 ID 无效");
        }
        int in = Json.integer(body, "maxInputTokens", -1), out = Json.integer(body, "maxOutputTokens", -1);
        if (in < 0 || in > 120000 || out < 0 || out > 6000)
            throw new ApiException(400, "INVALID_INPUT", "预留 token 无效");
        var price = budgets.pricing(a);
        var row = budgets.reserve(a, caller, id, BudgetService.estimate(price, in, out));
        return params("reservationId", row.get("id"), "status", row.get("status"), "pricingSnapshot", price, "pricingSignature", sign(row.get("id").toString(), price), "reservedAmount", row.get("reserved_amount"));
    }

    /**
     * 内部远程预算结算接口。
     * <p>
     * 在大模型调用结束后根据实际 Token 消耗执行最终核销。若状态为 SETTLED，必须通过 HMAC 数字签名比对校验传入的价格快照未遭篡改。
     *
     * @param r HTTP 请求对象
     * @param id 待结算的预扣流水 reservationId
     * @param body 请求负载（包含 status, inputTokens, outputTokens, pricingSnapshot, pricingSignature）
     * @return 包含 reservationId 与最新状态的 Map
     * @throws ApiException 400（缺少实际用量）、403（价格快照签名校验失败）
     */
    @PostMapping("/internal/budget/reservations/{id}/settle")
    public Object settle(HttpServletRequest r, @PathVariable String id, @RequestBody Map<String, Object> body) {
        var a = internal(r);
        String status = Json.str(body, "status");
        BigDecimal actual = null;
        if (status.equals("SETTLED")) {
            int in = Json.integer(body, "inputTokens", -1), out = Json.integer(body, "outputTokens", -1);
            if (in < 0 || out < 0) throw new ApiException(400, "INVALID_INPUT", "结算需要实际用量");
            var snapshot = body.get("pricingSnapshot") instanceof Map<?, ?> ? Json.object(Json.write(body.get("pricingSnapshot"))) : Map.<String, Object>of();
            String signature = Json.str(body, "pricingSignature");
            if (!MessageDigest.isEqual(sign(id, snapshot).getBytes(StandardCharsets.UTF_8), signature.getBytes(StandardCharsets.UTF_8)))
                throw new ApiException(403, "OPERATION_FORBIDDEN", "价格快照认证失败");
            actual = BudgetService.estimate(snapshot, in, out);
        }
        budgets.settle(a, id, status, actual);
        return params("reservationId", id, "status", status);
    }
}
